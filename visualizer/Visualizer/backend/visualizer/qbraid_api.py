"""Local HTTP API; every quote and submission resolves the allowlisted source again."""
from pathlib import Path
from typing import Literal
import math
import threading
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .qbraid_service import CloudError, CloudService, preflight_program
from .workers import run_isolated


class Selection(BaseModel):
    circuit_id: str = Field(min_length=1, max_length=128)
    step: int = Field(ge=0, le=100_000)
    step_kind: Literal['source', 'expanded'] = 'source'
    profile_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=200)
    shots: int = Field(default=100, ge=1, le=100_000)


class Submission(Selection):
    quote_id: uuid.UUID
    request_id: uuid.UUID
    acknowledge_unknown_pricing: bool = False
    ideal_probabilities: dict[str, float] | None = None

    @field_validator('ideal_probabilities')
    @classmethod
    def validate_ideal(cls, value):
        if value is None:
            return value
        if not value or len(value) > 4096:
            raise ValueError('Ideal comparison requires 1–4096 probabilities.')
        widths = {len(key) for key in value}
        if len(widths) != 1 or any(not key or set(key) - {'0', '1'} for key in value):
            raise ValueError('Ideal comparison must use equal-width binary strings.')
        if any(not math.isfinite(p) or p < 0 or p > 1 for p in value.values()) or abs(sum(value.values()) - 1) > 1e-6:
            raise ValueError('Ideal probabilities must be a complete normalized distribution.')
        return value


def create_qbraid_router(get_circuit, root: Path, service=None):
    router = APIRouter(prefix='/api/qbraid', tags=['qbraid'])
    # Lazy initialization leaves ordinary circuit exploration independent of cloud setup.
    holder = {'service': service}
    preparation_gate = threading.BoundedSemaphore(2)

    def cloud():
        if holder['service'] is None:
            holder['service'] = CloudService(root)
        return holder['service']

    def invoke(function, *args):
        try:
            return function(*args)
        except CloudError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    def selected(request):
        # get_circuit re-authorizes original bytes before any cloud estimate or submission.
        record = get_circuit(request.circuit_id)
        try:
            preflight_program(record, request.step, request.step_kind)
            if not preparation_gate.acquire(blocking=False):
                raise HTTPException(429, 'Two cloud program preparations are already running; try again shortly.')
            try:
                prepared, _ = run_isolated('cloud_prepare', {'record': {**record, 'id': request.circuit_id}, 'step': request.step, 'step_kind': request.step_kind}, 10)
            finally:
                preparation_gate.release()
        except CloudError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(408, 'Cloud program preparation exceeded 10 seconds; its worker was terminated.') from None
        except Exception:
            raise HTTPException(422, 'This checkpoint cannot be converted into a supported terminal-measurement cloud program. Select a prefix before measurement or classical control.') from None
        return prepared

    @router.get('/profiles')
    def profiles():
        return invoke(lambda: cloud().profile_list())

    @router.get('/profiles/{profile_id}/balance')
    def balance(profile_id: str):
        return invoke(lambda: cloud().balance(profile_id))

    @router.get('/profiles/{profile_id}/devices')
    def devices(profile_id: str):
        return invoke(lambda: cloud().devices(profile_id))

    @router.post('/quote')
    def quote(request: Selection):
        prepared = selected(request)
        return invoke(lambda: cloud().quote(request.model_dump(), prepared))

    @router.post('/submit')
    def submit(request: Submission):
        prepared = selected(request)
        if request.ideal_probabilities and any(len(b) != prepared['qubits'] for b in request.ideal_probabilities):
            raise HTTPException(422, 'Ideal comparison width does not match this checkpoint.')
        return invoke(lambda: cloud().submit(request.model_dump(mode='json'), prepared))

    @router.get('/jobs')
    def jobs():
        return {'jobs': invoke(lambda: cloud().jobs())}

    @router.get('/jobs/{request_id}')
    def job(request_id: uuid.UUID):
        return invoke(lambda: cloud().refresh(str(request_id)))

    return router
