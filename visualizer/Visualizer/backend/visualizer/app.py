from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import hashlib
import os
from pathlib import Path
import threading
import time
import tempfile
import codecs
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .access import AccessPolicy
from .circuits import prefix, prefix_at
from .examples import EXAMPLES
from .model_service import ModelService
from .dev_model import DevModelService
from .workers import run_isolated
from .inspector import native_parse, native_index, native_window, expanded_prefix, verified_inspector, WINDOW_SIZE
from .limits import DEFAULT_UPLOAD_BYTES, INTERACTIVE_BYTES, PYTHON_FALLBACK_BYTES, validate_upload_bytes, STATE_TIMEOUT_SECONDS, STATE_MEMORY_BYTES, STATE_VECTOR_QUBITS, STATE_STABILIZER_QUBITS, STATE_MPS_QUBITS, STATE_JOINT_QUBITS, STATE_OPERATIONS

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = DEFAULT_UPLOAD_BYTES
WINDOWS_CACHED = 4  # Inspected statement windows retained per windowed circuit (circuit cache holds 4 circuits).


class LRU:
    def __init__(self, size, dispose=None):
        self.size, self.data, self.lock = size, OrderedDict(), threading.RLock()
        self.dispose = dispose

    def get(self, key):
        with self.lock:
            if key not in self.data:
                return None
            self.data.move_to_end(key)
            return self.data[key]

    def put(self, key, value):
        with self.lock:
            previous = self.data.get(key)
            if previous is not None and previous is not value and self.dispose:
                self.dispose(previous)
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                _, removed = self.data.popitem(last=False)
                if self.dispose:
                    self.dispose(removed)

    def clear(self):
        with self.lock:
            if self.dispose:
                for value in self.data.values():
                    self.dispose(value)
            self.data.clear()


@dataclass
class Settings:
    root: Path = ROOT
    pipeline: Path = Path(os.environ.get('VISUALIZER_PIPELINE', str(ROOT.parent / 'ml-agent-pipeline')))
    predictor: Path = Path(os.environ.get('VISUALIZER_PREDICTOR', str(ROOT.parents[1])))
    model_provider: str = os.environ.get('VISUALIZER_MODEL', 'dev')
    state_timeout: float = STATE_TIMEOUT_SECONDS
    max_upload_bytes: int = field(default_factory=lambda: int(os.environ.get('VISUALIZER_MAX_UPLOAD_BYTES', DEFAULT_UPLOAD_BYTES)))
    large_feature_timeout: float = field(default_factory=lambda: float(os.environ.get('VISUALIZER_LARGE_FEATURE_TIMEOUT', '180')))
    large_feature_memory_bytes: int = field(default_factory=lambda: int(os.environ.get('VISUALIZER_LARGE_FEATURE_MEMORY_BYTES', str(3 * 1024 ** 3))))

    def __post_init__(self):
        validate_upload_bytes(self.max_upload_bytes)
        if isinstance(self.large_feature_timeout, bool) or not isinstance(self.large_feature_timeout, (int, float)) or not 1 <= self.large_feature_timeout <= 600:
            raise ValueError('VISUALIZER_LARGE_FEATURE_TIMEOUT must be between 1 and 600 seconds')
        if isinstance(self.large_feature_memory_bytes, bool) or not isinstance(self.large_feature_memory_bytes, int) or not 256 * 1024 ** 2 <= self.large_feature_memory_bytes <= 8 * 1024 ** 3:
            raise ValueError('VISUALIZER_LARGE_FEATURE_MEMORY_BYTES must be an integer from 268435456 through 8589934592')


class Checkpoint(BaseModel):
    step: int = Field(ge=0)
    step_kind: Literal['source', 'expanded'] = 'source'
    threshold: int = Field(default=256, ge=1, le=1_048_576)
    pair: tuple[int, int] = (0, 1)


class StateCheckpoint(Checkpoint):
    qubits: tuple[int, ...] | None = Field(default=None, min_length=1, max_length=3)
    method: Literal['auto', 'stabilizer', 'statevector', 'matrix_product_state'] = 'auto'
    include_joint: bool = False
    mps_max_bond: int = Field(default=64, ge=2, le=128)
    mps_cutoff: float = Field(default=1e-10, ge=0, le=1e-3, allow_inf_nan=False)


def create_app(settings=None, model=None):
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(application):
        local = settings.root / '.local'
        application.state.policy = AccessPolicy(settings.root / 'approved-development.json')
        if settings.model_provider not in {'dev', 'legacy'}:
            raise ValueError('VISUALIZER_MODEL must be dev or legacy')
        application.state.model = model or (DevModelService(settings.predictor, local, settings.root / 'runtime-checkpoint.json')
                                           if settings.model_provider == 'dev' else ModelService(settings.pipeline, local))
        application.state.file_lock = threading.RLock()
        application.state.active_files = set()
        application.state.pending_delete = set()
        def dispose(record):
            if record.get('source_path'):
                with application.state.file_lock:
                    path = record['source_path']
                    if path in application.state.active_files:
                        application.state.pending_delete.add(path)
                    else:
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            application.state.pending_delete.add(path)
        application.state.circuits = LRU(4, dispose)
        application.state.upload_directory = tempfile.TemporaryDirectory(prefix='visualizer-uploads-', ignore_cleanup_errors=True)
        application.state.upload_workers = threading.BoundedSemaphore(2)
        application.state.results = LRU(64)
        application.state.traces = LRU(4)
        application.state.workers = threading.BoundedSemaphore(2)
        application.state.large_workers = threading.BoundedSemaphore(1)
        application.state.state_workers = threading.BoundedSemaphore(1)
        try:
            _, application.state.inspector_build = verified_inspector(settings.root)
            application.state.inspector_error = None
        except (OSError, ValueError, KeyError) as exc:
            application.state.inspector_build = None
            application.state.inspector_error = str(exc)
        try:
            yield
        finally:
            application.state.circuits.clear()
            application.state.upload_directory.cleanup()
    app = FastAPI(title='Quantum Circuit Explorer', lifespan=lifespan)

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        # Bind to loopback and refuse cross-origin browser POSTs, including DNS rebinding.
        host = request.headers.get('host', '').split(':')[0]
        if host not in {'127.0.0.1', 'localhost', 'testserver'}:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={'detail': 'Local host only'})
        origin = request.headers.get('origin')
        allowed_origins = {'http://127.0.0.1:8000', 'http://localhost:8000', 'http://127.0.0.1:5173', 'http://localhost:5173', f'{request.url.scheme}://{request.headers.get("host")}' }
        if request.method == 'POST' and origin and origin not in allowed_origins:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=403, content={'detail': 'Cross-origin requests are disabled'})
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def circuit(cid):
        record = app.state.circuits.get(cid)
        if record is None:
            raise HTTPException(404, 'Circuit expired from the bounded session cache; upload or open it again')
        # Authorization is checked again at every computational entry point.
        try:
            app.state.policy.authorize_digest(cid)
            if record.get('source_path'):
                with Path(record['source_path']).open('rb') as stored:
                    if hashlib.file_digest(stored, 'sha256').hexdigest() != cid:
                        raise PermissionError('Stored upload content changed; upload the exact approved file again')
            elif hashlib.sha256(record['source'].encode('utf-8')).hexdigest() != cid:
                raise PermissionError('Stored circuit content changed; upload the exact approved file again')
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(404, 'Stored upload is no longer available; upload it again') from exc
        return record

    from .qbraid_api import create_qbraid_router
    cloud_ideals = {}
    def remember_cloud_ideal(cid, record, request, result):
        if result.get('global_joint_complete') is not True:
            return
        probabilities = result.get('ideal_probabilities')
        if probabilities is None:
            joint = result.get('joint', [])
            if not joint or result.get('joint_total', len(joint)) != len(joint):
                return
            probabilities = {r['bits']: r['probability'] for r in joint}
        if not isinstance(probabilities, dict) or not 1 <= len(probabilities) <= 4096:
            return
        if abs(sum(probabilities.values()) - 1) > 1e-6:
            return
        key = (cid, hashlib.sha256(record['source'].encode()).hexdigest(), request.step, request.step_kind)
        cloud_ideals[key] = probabilities
        while len(cloud_ideals) > 32:
            cloud_ideals.pop(next(iter(cloud_ideals)))
    def cloud_ideal(cid, prepared):
        point = prepared['ideal_checkpoint']
        return cloud_ideals.get((cid, prepared['source_hash'], point['step'], point['step_kind']))
    app.include_router(create_qbraid_router(circuit, settings.root, get_ideal=cloud_ideal))

    def compute(action, payload, timeout=10):
        if not app.state.workers.acquire(blocking=False):
            raise HTTPException(429, 'Two calculations are already running; try again shortly')
        try:
            return run_isolated(action, payload, timeout)
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            app.state.workers.release()

    def summary(cid, record):
        p = record['parsed']
        return {'id': cid, 'name': record['name'], 'origin': record['origin'],
                **{k: p[k] for k in ['num_qubits', 'num_clbits', 'total_steps', 'state_boundary', 'qubit_labels']},
                'parse_seconds': record['parse_seconds'], 'inspection_mode': p.get('inspection_mode', 'qiskit'),
                'inspection_backend': p.get('inspection_backend', 'python_source'),
                'inspector_fingerprint': p.get('inspector_fingerprint'), 'fallback_reason': p.get('fallback_reason'),
                'total_instructions': p.get('total_instructions', p['total_steps']),
                'expanded_state_boundary': p.get('expanded_state_boundary'),
                'source_bytes': record.get('source_bytes', len(record.get('source', ''))),
                'full_source_only': p.get('inspection_mode') == 'full_source_only',
                'warnings': [*p.get('warnings', []), 'Prefix runtime estimates are experimental.', 'No model prediction or state calculation has been run.'],
                **({'window_size': p['window_size'], 'windows': p['windows'], 'step_kind': 'source'} if p.get('inspection_mode') == 'windowed' else {})}

    def windowed_warnings(parsed):
        return [f"Windowed native inspection: {parsed['total_steps']:,} statements exceed the 100,000-statement interactive limit, so statements are inspected on demand in windows of {WINDOW_SIZE:,}. Only source-step navigation is available; expanded-instruction navigation is unavailable.",
                f'Features, prediction and SHAP use the exact source through the selected statement (the exact full source at the last statement). The native stepper keeps its 10,000-gate limit and state calculation keeps its own limits ({STATE_OPERATIONS:,} operations; {STATE_VECTOR_QUBITS} qubits statevector, {STATE_MPS_QUBITS} MPS, {STATE_STABILIZER_QUBITS} stabilizer). Cloud execution is unavailable in windowed mode.',
                f'A window whose {WINDOW_SIZE:,} statements expand to more than 100,000 primitive records (custom gates or register broadcasts) cannot be inspected and reports an explicit error.']

    def window_rows(record, k):
        # The per-circuit lock covers only the cache lookup and in-flight
        # registration. The native run takes one of the two worker slots
        # (429 when both are busy); a request for a window already being
        # inspected waits for that same inspection instead of starting another.
        with record['window_lock']:
            rows = record['window_cache'].get(k)
            if rows is not None:
                return rows
            pending = record['window_pending'].get(k)
            owner = pending is None
            if owner:
                pending = record['window_pending'][k] = {'event': threading.Event(), 'rows': None, 'error': None}
        window = record['parsed']['windows'][k]
        label = f"statements {window['start_step']:,}–{window['end_step']:,}"
        if not owner:
            if not pending['event'].wait(20):
                raise HTTPException(408, f'Windowed inspection of {label} is still running; try again shortly')
            if pending['error'] is not None:
                raise HTTPException(pending['error'].status_code, pending['error'].detail)
            if pending['rows'] is None:
                raise HTTPException(500, f'Windowed inspection of {label} failed unexpectedly')
            return pending['rows']
        try:
            if app.state.inspector_build is None:
                raise HTTPException(422, 'Native inspector unavailable at server startup')
            if not app.state.workers.acquire(blocking=False):
                raise HTTPException(429, 'Two calculations are already running; try again shortly')
            try:
                rows = native_window(record['source'], record['parsed'], k, settings.root, app.state.inspector_build['binary_sha256'],
                                     max_bytes=settings.max_upload_bytes)
            except TimeoutError as exc:
                raise HTTPException(408, f'Windowed inspection of {label} failed: {exc}') from exc
            except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
                raise HTTPException(422, f'Windowed inspection of {label} failed: {type(exc).__name__}: {exc}') from exc
            finally:
                app.state.workers.release()
            record['window_cache'].put(k, rows)
            pending['rows'] = rows
            return rows
        except HTTPException as exc:
            pending['error'] = exc
            raise
        finally:
            with record['window_lock']:
                record['window_pending'].pop(k, None)
            pending['event'].set()

    def windowed_step(record, step):
        if not 1 <= step <= record['parsed']['total_steps']:
            raise HTTPException(422, 'Step is outside this circuit')
        return window_rows(record, (step - 1) // WINDOW_SIZE)[(step - 1) % WINDOW_SIZE]

    def register(data, name, authorized=None):
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f'Upload exceeds the configured {settings.max_upload_bytes:,}-byte inspection limit')
        try:
            cid, approved = authorized or app.state.policy.authorize(data)
            if authorized is not None and hashlib.sha256(data).hexdigest() != cid:
                raise PermissionError('Staged upload content changed before parsing')
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        record = app.state.circuits.get(cid)
        if record is None:
            try:
                source = data.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise HTTPException(422, 'QASM must be UTF-8 text') from exc
            started = time.perf_counter()
            if not app.state.workers.acquire(blocking=False):
                raise HTTPException(429, 'Two calculations are already running; try again shortly')
            try:
                # Native subprocess has its own killable ten-second timeout. It
                # runs directly so terminating a Python worker cannot orphan it.
                if app.state.inspector_build is None:
                    raise ValueError('Native inspector unavailable at server startup: ' + app.state.inspector_error)
                parsed = native_parse(source, settings.root, app.state.inspector_build['binary_sha256'], max_bytes=settings.max_upload_bytes)
                fallback = None
            except (ValueError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as exc:
                fallback = f'{type(exc).__name__}: {exc}'
                if isinstance(exc, ValueError) and 'Source inspection limit' in str(exc) and len(data) <= INTERACTIVE_BYTES:
                    # Same worker slot: index every statement, inspect windows on demand.
                    try:
                        parsed = native_index(source, settings.root, app.state.inspector_build['binary_sha256'], max_bytes=settings.max_upload_bytes)
                        parsed.update(fallback_reason=fallback, warnings=windowed_warnings(parsed))
                        fallback = None
                    except (ValueError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as second:
                        fallback += f'; windowed native index failed: {type(second).__name__}: {second}'
            finally:
                app.state.workers.release()
            if fallback is not None:
                if len(data) > PYTHON_FALLBACK_BYTES:
                    status = 408 if fallback.startswith('TimeoutError:') else 422
                    raise HTTPException(status, 'Large-file native inspection failed: ' + fallback +
                                        '. Python SOURCE fallback is limited to 5 MiB; byte acceptance does not increase the 100,000-instruction or 2,048-qubit limits.')
                parsed, _ = compute('parse', {'qasm': source})
                parsed.update(inspection_backend='python_source', fallback_reason=fallback)
                parsed.setdefault('warnings', []).append('SOURCE view: native expansion unavailable. ' + fallback)
            elapsed = time.perf_counter() - started
            record = {'source': source, 'source_bytes': len(data), 'input_hash': cid, 'parsed': parsed, 'origin': approved['origin'], 'name': name, 'parse_seconds': elapsed}
            if parsed.get('inspection_mode') == 'windowed':
                record.update(window_cache=LRU(WINDOWS_CACHED), window_lock=threading.Lock(), window_pending={})
            app.state.circuits.put(cid, record)
        return summary(cid, record)

    def register_file(path, size, name, authorized):
        cid, approved = authorized
        cached = app.state.circuits.get(cid)
        if cached is not None:
            return summary(cid, circuit(cid))
        # Validate UTF-8 incrementally, after content authorization. No line or
        # statement parsing, string hydration, or Qiskit import for large files.
        decoder = codecs.getincrementaldecoder('utf-8')('strict')
        validated_hash = hashlib.sha256()
        try:
            with path.open('rb') as source_file:
                while chunk := source_file.read(1024 * 1024):
                    validated_hash.update(chunk)
                    decoder.decode(chunk)
                decoder.decode(b'', final=True)
        except UnicodeDecodeError as exc:
            raise HTTPException(422, 'QASM must be UTF-8 text') from exc
        if validated_hash.hexdigest() != cid:
            raise HTTPException(403, 'Staged upload content changed before validation')
        fallback_reason = None
        if size <= INTERACTIVE_BYTES:
            try:
                return register(path.read_bytes(), name, authorized)
            except HTTPException as exc:
                if size <= PYTHON_FALLBACK_BYTES or exc.status_code not in {408, 422}:
                    raise
                fallback_reason = exc.detail
        parsed = {'num_qubits': None, 'num_clbits': None, 'total_steps': 1, 'total_instructions': None,
                  'state_boundary': None, 'qubit_labels': [], 'steps': [], 'declarations': [],
                  'inspection_mode': 'full_source_only', 'inspection_backend': 'not_run_large_file',
                  'fallback_reason': fallback_reason,
                  'warnings': ['Full circuit mode: interactive inspection is unavailable for this file. Only exact full-source runtime features, prediction and SHAP are available. Syntax, gate counts and qubit counts have not been validated. Diagram, prefixes, state, stepper and cloud execution are unavailable.',
                               *(['Interactive inspection failed: ' + fallback_reason] if fallback_reason else ['File exceeds the 32 MiB interactive inspection budget.'])]}
        record = {'source_path': str(path), 'source_bytes': size, 'input_hash': cid, 'parsed': parsed,
                  'origin': approved['origin'], 'name': name, 'parse_seconds': 0}
        app.state.circuits.put(cid, record)
        return summary(cid, record)

    def selected(cid, request):
        record = circuit(cid)
        if record['parsed'].get('inspection_mode') == 'full_source_only':
            if request.step != 1 or request.step_kind != 'source':
                raise HTTPException(422, 'Large-file mode supports only the Full circuit checkpoint (source step 1); arbitrary prefixes are unavailable')
            return record, Path(record['source_path'])
        if record['parsed'].get('inspection_mode') == 'windowed':
            if request.step_kind != 'source':
                raise HTTPException(422, 'Expanded checkpoints are unavailable in windowed native inspection; select a source step')
            total = record['parsed']['total_steps']
            if not 0 <= request.step <= total:
                raise HTTPException(422, 'Step is outside this circuit')
            if request.step == total:
                return record, record['source']  # Exact full-source featurization.
            cutoff = windowed_step(record, request.step)['end'] if request.step else 0
            return record, prefix_at(record['source'], record['parsed']['declarations'], cutoff)
        try:
            if request.step_kind == 'expanded':
                source, _ = expanded_prefix(record['source'], record['parsed'], request.step)
            else:
                source = prefix(record['source'], record['parsed'], request.step)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return record, source

    def checkpoint(record, request, source):
        p = record['parsed']
        if p.get('inspection_mode') == 'full_source_only':
            return {'step': 1, 'step_kind': 'source', 'prefix_kind': 'original_full_source',
                    'checkpoint_label': 'Full circuit', 'input_hash': record['input_hash'],
                    'prefix_hash': record['input_hash'], 'source_origin': None}
        if p.get('inspection_mode') == 'windowed':
            # The exact full source needs no window inspection (a window above
            # the record limit must not block full-circuit prediction).
            origin = windowed_step(record, request.step) if 0 < request.step < p['total_steps'] else None
        else:
            records = p.get('instructions', []) if request.step_kind == 'expanded' else p['steps']
            origin = records[request.step - 1] if request.step else None
        if request.step_kind == 'expanded':
            _, kind = expanded_prefix(record['source'], p, request.step)
        else:
            kind = 'original_full_source' if request.step == p['total_steps'] else 'source_boundary'
        return {'step': request.step, 'step_kind': request.step_kind, 'prefix_kind': kind,
                'input_hash': record['input_hash'],
                'prefix_hash': hashlib.sha256(source.encode()).hexdigest(),
                'source_origin': ({k: origin[k] for k in ['source', 'line', 'start', 'end']} | {'source_step': origin.get('source_step', request.step)}) if origin else None}

    def provenance(record, values=None):
        p = record['parsed']
        result = {'inspection_backend': p.get('inspection_backend', 'python_source'),
                  'inspection_mode': p.get('inspection_mode', 'qiskit'), 'inspection_fallback_reason': p.get('fallback_reason'),
                  'prediction_backend': 'python_four_histgbdt_blend' if app.state.model.info.get('provider') == 'qasm-runtime-predictor' else app.state.model.info.get('provider', 'python_model')}
        if values is not None:
            status = values.get('st_ok')
            result.update(st_ok=status, truncated=values.get('st_truncated'),
                          structural_feature_backend='native_cpp' if status == 1 else 'python_fallback' if status in (.25, .5, .75) else 'unavailable_or_unknown',
                          structural_feature_status='complete' if status in (1, .75) and not values.get('st_truncated') else 'partial_or_unavailable',
                          fallback_reason=None)  # Upstream discards the native-failure reason.
        return result

    def cache_identity(record, source):
        return (record['input_hash'],
                record['input_hash'] if isinstance(source, Path) else hashlib.sha256(source.encode()).hexdigest(), app.state.model.info.get('fingerprint'))

    def validate_threshold(request):
        thresholds = app.state.model.info.get('thresholds')
        if thresholds is not None and request.threshold not in thresholds:
            raise HTTPException(422, f'This checkpoint supports simulator thresholds {thresholds}')

    @app.get('/api/health')
    def health():
        try:
            _, build = verified_inspector(settings.root)
            if app.state.inspector_build is None or build['binary_sha256'] != app.state.inspector_build['binary_sha256']:
                raise ValueError('Inspector differs from server startup; restart with a verified build')
            inspector = {'available': True, 'backend': 'app_owned_cpp', 'fingerprint': build['binary_sha256'], 'upstream_commit': build['commit']}
        except (OSError, ValueError, KeyError) as exc:
            inspector = {'available': False, 'error': str(exc), 'fallback': 'python_source'}
        return {'ok': True, 'application_root': str(settings.root.resolve()), 'model': app.state.model.info, 'access': {'scope': 'development_only', 'approved_files': len(app.state.policy.entries), 'source': app.state.policy.source},
                'inspection': inspector, 'limits': {'statevector_qubits': STATE_VECTOR_QUBITS, 'state_stabilizer_qubits': STATE_STABILIZER_QUBITS, 'state_mps_qubits': STATE_MPS_QUBITS, 'state_joint_qubits': STATE_JOINT_QUBITS, 'state_operations': STATE_OPERATIONS, 'state_memory_bytes': STATE_MEMORY_BYTES, 'state_workers': 1, 'inspection_operations': 100_000, 'inspection_qubits': 2048, 'nesting': 64, 'workers': 2, 'inspection_attempt_timeout_seconds': 10, 'state_timeout_seconds': settings.state_timeout, 'upload_bytes': settings.max_upload_bytes, 'interactive_bytes': INTERACTIVE_BYTES, 'python_fallback_bytes': PYTHON_FALLBACK_BYTES, 'large_feature_timeout_seconds': settings.large_feature_timeout, 'large_feature_memory_bytes': settings.large_feature_memory_bytes, 'large_feature_workers': 1, 'native_output_bytes': 64 * 1024 * 1024, 'metadata_preview_chars': 4096, 'window_size': WINDOW_SIZE, 'windows_cached': WINDOWS_CACHED}}

    @app.get('/api/examples')
    def examples():
        return [{'id': key, 'title': value['title'], 'description': value['description']} for key, value in EXAMPLES.items()]

    @app.post('/api/examples/{key}')
    def example(key: str):
        if key not in EXAMPLES:
            raise HTTPException(404, 'Unknown example')
        return register(EXAMPLES[key]['qasm'].encode(), EXAMPLES[key]['title'])

    @app.get('/api/examples/{key}/source')
    def example_source(key: str):
        if key not in EXAMPLES:
            raise HTTPException(404, 'Unknown example')
        from fastapi.responses import Response
        return Response(EXAMPLES[key]['qasm'], media_type='text/plain', headers={'Content-Disposition': f'attachment; filename="{key}.qasm"'})

    @app.post('/api/circuits')
    async def upload(request: Request, name: str = Query(default='uploaded.qasm', max_length=200)):
        digest = hashlib.sha256()
        length = request.headers.get('content-length')
        if length and length.isdigit() and int(length) > settings.max_upload_bytes:
            raise HTTPException(413, f'Upload exceeds the configured {settings.max_upload_bytes:,}-byte inspection limit')
        path = Path(app.state.upload_directory.name) / (os.urandom(16).hex() + '.qasm')
        size = 0
        retained = False
        if not app.state.upload_workers.acquire(blocking=False):
            raise HTTPException(429, 'Two uploads are already streaming; try again shortly')
        try:
            with path.open('wb') as output:
                async for chunk in request.stream():
                    if len(chunk) > settings.max_upload_bytes - size:
                        raise HTTPException(413, f'Upload exceeds the configured {settings.max_upload_bytes:,}-byte inspection limit')
                    output.write(chunk)
                    size += len(chunk)
                    digest.update(chunk)
            # authorize before decoding/parsing or dispatching computation.
            try:
                authorized = app.state.policy.authorize_digest(digest.hexdigest())
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(register_file, path, size, Path(name).name, authorized)
            cached = app.state.circuits.get(result['id'])
            retained = cached is not None and cached.get('source_path') == str(path)
            return result
        finally:
            if not retained:
                path.unlink(missing_ok=True)
            app.state.upload_workers.release()

    @app.get('/api/circuits/{cid}/steps')
    def steps(cid: str, offset: int = Query(0, ge=0), limit: int = Query(80, ge=1, le=200)):
        record = circuit(cid)
        p = record['parsed']
        if p.get('inspection_mode') == 'full_source_only':
            raise HTTPException(422, 'Gate navigation is unavailable in large-file Full circuit mode')
        if p.get('inspection_mode') == 'windowed':
            # Global 1-based steps; window k covers k*WINDOW_SIZE+1 ... (k+1)*WINDOW_SIZE.
            end, rows = min(offset + limit, p['total_steps']), []
            for k in range(offset // WINDOW_SIZE, (end - 1) // WINDOW_SIZE + 1) if offset < end else ():
                rows.extend(window_rows(record, k)[max(offset, k * WINDOW_SIZE) - k * WINDOW_SIZE:end - k * WINDOW_SIZE])
            return {'offset': offset, 'total': p['total_steps'], 'steps': rows}
        return {'offset': offset, 'total': p['total_steps'], 'steps': p['steps'][offset:offset + limit]}

    @app.get('/api/circuits/{cid}/instructions')
    def instructions(cid: str, offset: int = Query(0, ge=0), limit: int = Query(80, ge=1, le=200)):
        record = circuit(cid)
        p = record['parsed']
        if p.get('inspection_mode') == 'full_source_only':
            raise HTTPException(422, 'Instruction navigation is unavailable in large-file Full circuit mode')
        if p.get('inspection_mode') == 'windowed':
            raise HTTPException(422, 'Expanded instruction navigation is unavailable in windowed native inspection; use source steps (/steps)')
        rows = p.get('instructions', p['steps'])
        return {'offset': offset, 'total': len(rows), 'steps': rows[offset:offset + limit],
                'step_kind': 'expanded' if 'instructions' in p else 'source', 'provenance': provenance(record)}

    @app.get('/api/circuits/{cid}/overview')
    def overview(cid: str, buckets: int = Query(100, ge=1, le=200)):
        record = circuit(cid)
        p = record['parsed']
        if p.get('inspection_mode') == 'full_source_only':
            raise HTTPException(422, 'Diagram overview is unavailable in large-file Full circuit mode')
        if p.get('inspection_mode') == 'windowed':
            # Statement counts per equal-width bucket, from the native index total
            # (the index's bucket_counts are this arithmetic at buckets=200).
            total = p['total_steps']
            width = max(1, (total + buckets - 1) // buckets)
            return {'total': total, 'step_kind': 'source',
                    'buckets': [{'start': start + 1, 'end': min(start + width, total), 'count': min(width, total - start)}
                                for start in range(0, total, width)], 'provenance': provenance(record)}
        rows = p.get('instructions', p['steps'])
        width = max(1, (len(rows) + buckets - 1) // buckets)
        return {'total': len(rows), 'step_kind': 'expanded' if 'instructions' in p else 'source',
                'buckets': [{'start': start + 1, 'end': min(start + width, len(rows)), 'count': len(rows[start:start + width])}
                            for start in range(0, len(rows), width)], 'provenance': provenance(record)}

    @app.post('/api/circuits/{cid}/features')
    def features(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
        validate_threshold(request)
        metadata = checkpoint(record, request, source)
        cache_key = ('features', *cache_identity(record, source))
        cached = app.state.results.get(cache_key)
        if cached is not None:
            result = {**cached, **metadata, 'cached': True}
            derived = app.state.model.derived(cached['features'], request.threshold) if hasattr(app.state.model, 'derived') else {}
            result.update(threshold=request.threshold, derived_features=derived,
                          model_inputs=app.state.model.inputs(cached['features'], request.threshold) if hasattr(app.state.model, 'inputs') else {**cached['features'], **derived})
            return result
        try:
            if isinstance(source, Path):
                if not hasattr(app.state.model, 'features_file'):
                    raise ValueError('This model provider does not support isolated full-file extraction')
                if not app.state.large_workers.acquire(blocking=False):
                    raise HTTPException(429, 'One large-file feature calculation is already running; try again shortly')
                try:
                    with app.state.file_lock:
                        app.state.active_files.add(str(source))
                    values = app.state.model.features_file(source, record['input_hash'], settings.large_feature_timeout,
                                                          memory_bytes=settings.large_feature_memory_bytes)
                finally:
                    with app.state.file_lock:
                        app.state.active_files.discard(str(source))
                        if str(source) in app.state.pending_delete:
                            source.unlink(missing_ok=True)
                            app.state.pending_delete.discard(str(source))
                    app.state.large_workers.release()
            else:
                values = app.state.model.features(source)
            diagnostics = list(record['parsed'].get('warnings', []))
            for left, right in [('n_qubits', 'st_n_qubits'), ('n_gates', 'st_n_gates'),
                                ('n_2q', 'st_n_2q'), ('n_measure', 'st_n_measure'), ('n_reset', 'st_n_reset')]:
                if left in values and right in values and values[left] != values[right]:
                    diagnostics.append(f'Upstream extractor mismatch: {left}={values[left]}, {right}={values[right]}. Features preserved unchanged.')
            if 'st_ok' in values and (values['st_ok'] != 1 or values.get('st_truncated')):
                diagnostics.append('Native pass incomplete or fallback used; prediction preserves upstream features. Native trace is unavailable.')
            if values.get('st_n_skipped_stmts') or values.get('st_n_param_parse_err') or values.get('st_n_unknown_gates'):
                diagnostics.append('Native parser reports skipped statements, parameter errors, or unknown gates; inspect the st_* counters. Features preserved unchanged.')
            if record['parsed'].get('inspection_mode') == 'windowed':
                # Global instance count through the selected statement (index offsets;
                # the native index total for the exact full source).
                total = record['parsed']['total_steps']
                imported = record['parsed']['_total_instances'] if request.step == total else windowed_step(record, request.step)['op_end'] if request.step else 0
            else:
                source_steps = (record['parsed'].get('instructions', []) if request.step_kind == 'expanded' else record['parsed']['steps'])[:request.step]
                imported = sum(step['instances'] for step in source_steps)
            if not isinstance(source, Path) and 'st_n_gates' in values and imported != values['st_n_gates']:
                diagnostics.append(f"Source/imported instruction count {imported} differs from native expanded gate count {values['st_n_gates']}. Custom calls expand; measurement, reset and barriers have separate counters. Features preserved unchanged.")
            result = {'features': values, 'raw_features': values, 'diagnostics': diagnostics,
                      'provenance': provenance(record, values), 'cached': False}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        app.state.results.put(cache_key, result)
        derived = app.state.model.derived(values, request.threshold) if hasattr(app.state.model, 'derived') else {}
        return {**result, **metadata, 'threshold': request.threshold, 'derived_features': derived,
                'model_inputs': app.state.model.inputs(values, request.threshold) if hasattr(app.state.model, 'inputs') else {**values, **derived}}

    @app.post('/api/circuits/{cid}/predict')
    def predict(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
        validate_threshold(request)
        metadata = checkpoint(record, request, source)
        key = ('predict', *cache_identity(record, source), request.threshold)
        cached = app.state.results.get(key)
        if cached is not None:
            return {**cached, **metadata, 'cached': True}
        started = time.perf_counter()
        extracted = features(cid, request)
        values = extracted['features']
        try:
            seconds = app.state.model.predict(values, request.threshold)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = {**metadata, 'threshold': request.threshold, 'predicted_seconds': seconds, 'features': values,
                  'raw_features': extracted['raw_features'], 'derived_features': extracted['derived_features'],
                  'model_inputs': extracted['model_inputs'], 'provenance': extracted['provenance'],
                  'diagnostics': extracted.get('diagnostics', []), 'model_fingerprint': app.state.model.info.get('fingerprint'), 'inference_seconds': time.perf_counter() - started, 'cached': False,
                  'note': 'Fresh run of the circuit prefix. Experimental estimate; prediction differences are not measured gate costs.'}
        app.state.results.put(key, result)
        return result

    @app.post('/api/circuits/{cid}/state')
    def state(cid: str, request: StateCheckpoint):
        record, source = selected(cid, request)
        if isinstance(source, Path):
            raise HTTPException(422, 'State calculation is unavailable in large-file Full circuit mode; interactive inspection is limited to 32 MiB')
        boundary = record['parsed'].get('expanded_state_boundary') if request.step_kind == 'expanded' else record['parsed']['state_boundary']
        if boundary is not None and request.step >= boundary:
            raise HTTPException(422, f'State inspection stops before step {boundary}: measurement or reset')
        metadata = checkpoint(record, request, source)
        selection = request.qubits if request.qubits is not None else request.pair
        if request.qubits is None and record['parsed']['num_qubits'] == 1:
            selection = (0,)
        key = ('state-v2', *cache_identity(record, source), selection, request.method, request.include_joint, request.mps_max_bond, request.mps_cutoff)
        cached = app.state.results.get(key)
        if cached is not None:
            remember_cloud_ideal(cid, record, request, cached)
            return {**cached, **metadata, 'cached': True}
        if not app.state.state_workers.acquire(blocking=False):
            raise HTTPException(429, 'One state calculation is already running; try again shortly')
        try:
            result, elapsed = compute('state', {'qasm': source, 'qubits': list(selection), 'method': request.method, 'include_joint': request.include_joint, 'mps_max_bond': request.mps_max_bond, 'mps_cutoff': request.mps_cutoff}, settings.state_timeout)
        finally:
            app.state.state_workers.release()
        result.update({**metadata, 'provenance': provenance(record), 'calculation_seconds': elapsed, 'cached': False})
        app.state.results.put(key, result)
        remember_cloud_ideal(cid, record, request, result)
        return result

    @app.post('/api/circuits/{cid}/stepper')
    def stepper(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
        if isinstance(source, Path):
            raise HTTPException(422, 'Instruction stepping is unavailable in large-file Full circuit mode')
        validate_threshold(request)
        if not app.state.model.info.get('stepper_available'):
            raise HTTPException(422, 'Native C++ stepper is unavailable for this checkpoint')
        metadata = checkpoint(record, request, source)
        key = (*cache_identity(record, source), request.threshold)
        cached = app.state.traces.get(key)
        if cached is not None:
            return {**cached, **metadata, 'cached': True}
        if not app.state.workers.acquire(blocking=False):
            raise HTTPException(429, 'Two calculations are already running; try again shortly')
        try:
            extracted = features(cid, request)
            values = extracted['features']
            result = app.state.model.trace(source, request.threshold, values)
            if extracted.get('diagnostics'):
                result['note'] += ' ' + ' '.join(extracted['diagnostics'])
        except TimeoutError as exc:
            raise HTTPException(408, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            app.state.workers.release()
        result.update({**metadata, 'provenance': extracted['provenance'], 'cached': False})
        app.state.traces.put(key, result)
        return result

    @app.post('/api/circuits/{cid}/explain')
    def explain(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
        validate_threshold(request)
        if app.state.model.info.get('explanation_available') is False:
            raise HTTPException(422, 'Explanation unavailable: ' + app.state.model.info.get('explanation_error', 'Tree SHAP is unsupported for this checkpoint'))
        metadata = checkpoint(record, request, source)
        key = ('explain', *cache_identity(record, source), request.threshold)
        cached = app.state.results.get(key)
        if cached is not None:
            return {**cached, **metadata, 'cached': True}
        extracted = features(cid, request)
        values = extracted['features']
        try:
            result = app.state.model.explain(values, request.threshold)
        except Exception as exc:
            raise HTTPException(422, f'Explanation unavailable: {exc}') from exc
        result.update({**metadata, 'threshold': request.threshold, 'model_fingerprint': app.state.model.info.get('fingerprint'), 'provenance': extracted['provenance'], 'cached': False})
        app.state.results.put(key, result)
        return result

    @app.get('/api/importance')
    def importance():
        # Only a separately approved, development-validation-derived report may be shown.
        path = settings.root / 'approved-importance.json'
        if not path.is_file():
            return {'available': False, 'note': 'No approved development-validation importance report is installed. Training impurity importance is not substituted. No evaluation is run by this app.'}
        report = json.loads(path.read_text())
        if report.get('scope') != 'development_only' or report.get('method') != 'permutation' or report.get('model_fingerprint') != app.state.model.info.get('fingerprint'):
            raise HTTPException(422, 'Importance report provenance or checkpoint does not match')
        return {'available': True, **report}

    dist = settings.root / 'frontend' / 'dist'
    if dist.is_dir():
        app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')
        @app.get('/')
        def index():
            return FileResponse(dist / 'index.html')
    return app


app = create_app()
