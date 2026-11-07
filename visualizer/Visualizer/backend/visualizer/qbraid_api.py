"""Local HTTP API; every quote and submission resolves the allowlisted source again."""
from pathlib import Path
from typing import Literal
import json
import math
import threading
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from .qbraid_service import CloudError, CloudService, preflight_program
from .workers import run_isolated


class Selection(BaseModel):
    circuit_id: str = Field(min_length=1, max_length=128)
    step: int = Field(default=0, ge=0, le=100_000)
    scope: Literal['full', 'checkpoint'] = 'full'
    acknowledge_initial_state: bool = False
    runtime_options: dict = Field(default_factory=dict)
    step_kind: Literal['source', 'expanded'] = 'source'
    profile_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=200)
    shots: int = Field(default=100, ge=1, le=100_000)


class Submission(Selection):
    quote_id: uuid.UUID
    request_id: uuid.UUID
    acknowledge_unknown_pricing: bool = False
    ideal_probabilities: dict[str, float] | None = None
    ideal_program_hash: str | None = Field(default=None, max_length=64)
    ideal_step: int | None = None
    ideal_step_kind: Literal['source', 'expanded'] | None = None

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


def create_qbraid_router(get_circuit, root: Path, service=None, get_ideal=None):
    router = APIRouter(prefix='/api/qbraid', tags=['qbraid'])
    # Lazy initialization leaves ordinary circuit exploration independent of cloud setup.
    holder = {'service': service}
    initialization_lock = threading.Lock()
    preparation_gate = threading.BoundedSemaphore(2)
    connection_gate = threading.BoundedSemaphore(1)

    def cloud():
        with initialization_lock:
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
            preflight_program(record, request.step, request.step_kind, request.scope, request.acknowledge_initial_state)
            if not preparation_gate.acquire(blocking=False):
                raise HTTPException(429, 'Two cloud program preparations are already running; try again shortly.')
            try:
                prepared, _ = run_isolated('cloud_prepare', {'record': {**record, 'id': request.circuit_id}, 'step': request.step, 'step_kind': request.step_kind, 'scope': request.scope, 'acknowledge_initial_state': request.acknowledge_initial_state}, 10)
            finally:
                preparation_gate.release()
        except CloudError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        except HTTPException:
            raise
        except TimeoutError:
            raise HTTPException(408, 'Cloud program preparation exceeded 10 seconds; its worker was terminated.') from None
        except Exception:
            raise HTTPException(422, 'This scope cannot be prepared. Full circuits must have only terminal measurements and no reset or classical control; choose a checkpoint before unsupported operations.') from None
        prepared['ideal_available'] = bool(get_ideal and get_ideal(request.circuit_id, prepared))
        return prepared

    @router.get('/profiles')
    def profiles():
        return invoke(lambda: cloud().profile_list())

    @router.post('/connect')
    async def connect(request: Request, response: Response):
        # Parse this secret-bearing body ourselves: default validation errors may
        # serialize rejected input. Bound even streamed/chunked request bodies.
        response.headers['Cache-Control'] = 'no-store'
        def invalid():
            return HTTPException(422, 'Enter a valid qBraid API key (1–4096 characters, without whitespace).', headers={'Cache-Control': 'no-store'})
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 8192:
                raise invalid()
            body.extend(chunk)
        try:
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) - {'api_key', 'profile_id'}:
                raise ValueError()
            token, pid = data.get('api_key'), data.get('profile_id')
            if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c.isspace() for c in token):
                raise ValueError()
            if pid is not None and (not isinstance(pid, str) or not 1 <= len(pid) <= 64):
                raise ValueError()
        except (ValueError, TypeError):
            raise invalid() from None
        finally:
            body.clear()
        if not connection_gate.acquire(blocking=False):
            raise HTTPException(429, 'A local qBraid connection is already being checked. Try again shortly.', headers={'Cache-Control': 'no-store'})
        try:
            return await run_in_threadpool(lambda: cloud().connect(token, pid))
        except CloudError as exc:
            raise HTTPException(exc.status, str(exc), headers={'Cache-Control': 'no-store'}) from None
        finally:
            connection_gate.release()

    @router.post('/profiles/{profile_id}/disconnect')
    def disconnect(profile_id: str, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return invoke(lambda: cloud().disconnect(profile_id))

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
        data = request.model_dump(mode='json')
        # Compare only a complete distribution already computed by this server's Qiskit state view.
        # Never trust caller-supplied probabilities and never trigger another state calculation.
        data['ideal_probabilities'] = get_ideal(request.circuit_id, prepared) if get_ideal else None
        return invoke(lambda: cloud().submit(data, prepared))

    class Comparison(BaseModel):
        request_ids: list[uuid.UUID] = Field(min_length=2, max_length=4)

    @router.post('/compare')
    def compare(request: Comparison):
        return invoke(lambda: cloud().compare_jobs([str(rid) for rid in request.request_ids]))

    @router.post('/jobs/{request_id}/programs')
    def programs(request_id: uuid.UUID):
        return invoke(lambda: cloud().fetch_programs(str(request_id)))

    @router.get('/jobs')
    def jobs():
        return {'jobs': invoke(lambda: cloud().jobs())}

    @router.get('/jobs/{request_id}')
    def job(request_id: uuid.UUID):
        return invoke(lambda: cloud().refresh(str(request_id)))

    return router
