from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .access import AccessPolicy
from .circuits import prefix
from .examples import EXAMPLES
from .model_service import ModelService
from .dev_model import DevModelService
from .workers import run_isolated
from .inspector import native_parse, expanded_prefix, verified_inspector

ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 5 * 1024 * 1024


class LRU:
    def __init__(self, size):
        self.size, self.data, self.lock = size, OrderedDict(), threading.RLock()

    def get(self, key):
        with self.lock:
            if key not in self.data:
                return None
            self.data.move_to_end(key)
            return self.data[key]

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


@dataclass
class Settings:
    root: Path = ROOT
    pipeline: Path = Path(os.environ.get('VISUALIZER_PIPELINE', str(ROOT.parent / 'ml-agent-pipeline')))
    predictor: Path = Path(os.environ.get('VISUALIZER_PREDICTOR', str(ROOT.parent / 'quantum-runtime-prediction-model' / 'qasm-runtime-predictor')))
    model_provider: str = os.environ.get('VISUALIZER_MODEL', 'dev')
    state_timeout: float = 10


class Checkpoint(BaseModel):
    step: int = Field(ge=0)
    step_kind: Literal['source', 'expanded'] = 'source'
    threshold: int = Field(default=256, ge=1, le=1_048_576)
    pair: tuple[int, int] = (0, 1)


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
        application.state.circuits = LRU(4)
        application.state.results = LRU(64)
        application.state.traces = LRU(4)
        application.state.workers = threading.BoundedSemaphore(2)
        try:
            _, application.state.inspector_build = verified_inspector(settings.root)
            application.state.inspector_error = None
        except (OSError, ValueError, KeyError) as exc:
            application.state.inspector_build = None
            application.state.inspector_error = str(exc)
        yield
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
            app.state.policy.authorize(record['source'].encode())
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return record

    from .qbraid_api import create_qbraid_router
    app.include_router(create_qbraid_router(circuit, settings.root))

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
                'warnings': [*p.get('warnings', []), 'Prefix runtime estimates are experimental.', 'No model prediction or state calculation has been run.']}

    def register(data, name):
        try:
            cid, approved = app.state.policy.authorize(data)
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
                parsed = native_parse(source, settings.root, app.state.inspector_build['binary_sha256'])
                fallback = None
            except (ValueError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as exc:
                fallback = f'{type(exc).__name__}: {exc}'
            finally:
                app.state.workers.release()
            if fallback is not None:
                parsed, _ = compute('parse', {'qasm': source})
                parsed.update(inspection_backend='python_source', fallback_reason=fallback)
                parsed.setdefault('warnings', []).append('SOURCE view: native expansion unavailable. ' + fallback)
            elapsed = time.perf_counter() - started
            record = {'source': source, 'parsed': parsed, 'origin': approved['origin'], 'name': name, 'parse_seconds': elapsed}
            app.state.circuits.put(cid, record)
        return summary(cid, record)

    def selected(cid, request):
        record = circuit(cid)
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
        records = p.get('instructions', []) if request.step_kind == 'expanded' else p['steps']
        origin = records[request.step - 1] if request.step else None
        if request.step_kind == 'expanded':
            _, kind = expanded_prefix(record['source'], p, request.step)
        else:
            kind = 'original_full_source' if request.step == p['total_steps'] else 'source_boundary'
        return {'step': request.step, 'step_kind': request.step_kind, 'prefix_kind': kind,
                'input_hash': hashlib.sha256(record['source'].encode()).hexdigest(),
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
        return (hashlib.sha256(record['source'].encode()).hexdigest(),
                hashlib.sha256(source.encode()).hexdigest(), app.state.model.info.get('fingerprint'))

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
        return {'ok': True, 'model': app.state.model.info, 'access': {'scope': 'development_only', 'approved_files': len(app.state.policy.entries), 'source': app.state.policy.source},
                'inspection': inspector, 'limits': {'state_qubits': 12, 'state_operations': 10_000, 'inspection_operations': 100_000, 'inspection_qubits': 2048, 'nesting': 64, 'workers': 2, 'inspection_attempt_timeout_seconds': 10, 'state_timeout_seconds': settings.state_timeout, 'upload_bytes': MAX_BYTES}}

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
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, 'Upload exceeds the 5 MiB inspection limit')
        # authorize before parsing, before dispatching any computation.
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(register, bytes(data), Path(name).name)

    @app.get('/api/circuits/{cid}/steps')
    def steps(cid: str, offset: int = Query(0, ge=0), limit: int = Query(80, ge=1, le=200)):
        p = circuit(cid)['parsed']
        return {'offset': offset, 'total': p['total_steps'], 'steps': p['steps'][offset:offset + limit]}

    @app.get('/api/circuits/{cid}/instructions')
    def instructions(cid: str, offset: int = Query(0, ge=0), limit: int = Query(80, ge=1, le=200)):
        record = circuit(cid)
        p = record['parsed']
        rows = p.get('instructions', p['steps'])
        return {'offset': offset, 'total': len(rows), 'steps': rows[offset:offset + limit],
                'step_kind': 'expanded' if 'instructions' in p else 'source', 'provenance': provenance(record)}

    @app.get('/api/circuits/{cid}/overview')
    def overview(cid: str, buckets: int = Query(100, ge=1, le=200)):
        record = circuit(cid)
        p = record['parsed']
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
            source_steps = (record['parsed'].get('instructions', []) if request.step_kind == 'expanded' else record['parsed']['steps'])[:request.step]
            imported = sum(step['instances'] for step in source_steps)
            if 'st_n_gates' in values and imported != values['st_n_gates']:
                diagnostics.append(f"Source/imported instruction count {imported} differs from native expanded gate count {values['st_n_gates']}. Custom calls expand; measurement, reset and barriers have separate counters. Features preserved unchanged.")
            result = {'features': values, 'raw_features': values, 'diagnostics': diagnostics,
                      'provenance': provenance(record, values), 'cached': False}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
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
    def state(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
        boundary = record['parsed'].get('expanded_state_boundary') if request.step_kind == 'expanded' else record['parsed']['state_boundary']
        if boundary is not None and request.step >= boundary:
            raise HTTPException(422, f'State inspection stops before step {boundary}: measurement or reset')
        if record['parsed']['num_qubits'] > 12:
            raise HTTPException(422, 'Exact state calculation is limited to 12 qubits')
        metadata = checkpoint(record, request, source)
        key = ('state', *cache_identity(record, source), request.pair)
        cached = app.state.results.get(key)
        if cached is not None:
            return {**cached, **metadata, 'cached': True}
        result, elapsed = compute('state', {'qasm': source, 'pair': list(request.pair)}, settings.state_timeout)
        result.update({**metadata, 'provenance': provenance(record), 'calculation_seconds': elapsed, 'cached': False})
        app.state.results.put(key, result)
        return result

    @app.post('/api/circuits/{cid}/stepper')
    def stepper(cid: str, request: Checkpoint):
        record, source = selected(cid, request)
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
