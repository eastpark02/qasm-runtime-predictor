"""Killable, spawned computation. Windows-safe; no executor cancellation fiction."""
import multiprocessing as mp
import time


def _full_file_features(payload):
    import hashlib
    import importlib.util
    import json
    import math
    from pathlib import Path
    import sys
    import contextlib
    import io
    from .process_guard import enter_resource_job

    resource_job = enter_resource_job(payload['memory_bytes'])
    snapshot = Path(payload['snapshot'])
    manifest = json.loads((snapshot / 'checkpoint.json').read_text())
    for name, expected in manifest['files'].items():
        path = snapshot / ('artifacts/model.pkl' if name == 'artifacts/model.pkl' else Path(name).name)
        with path.open('rb') as data:
            if hashlib.file_digest(data, 'sha256').hexdigest() != expected:
                raise ValueError('Pinned feature snapshot changed: ' + name)
    with (snapshot / 'qasm_step.exe').open('rb') as binary:
        if hashlib.file_digest(binary, 'sha256').hexdigest() != payload['binary_sha256']:
            raise ValueError('Pinned feature binary changed')
    # Decode exactly once in the isolated, memory-limited process. No source
    # normalization, truncation, Qiskit conversion, or feature substitutions.
    source_path = Path(payload['source_path'])
    with source_path.open('rb') as source_file:
        if hashlib.file_digest(source_file, 'sha256').hexdigest() != payload['input_hash']:
            raise ValueError('Stored upload changed before full-source extraction')
        source_file.seek(0)
        source = source_file.read().decode('utf-8')
    sys.path.insert(0, str(snapshot))
    for name in ('qasm_features', 'qasm_step_py', 'qasm_step'):
        spec = importlib.util.spec_from_file_location(name, snapshot / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    spec = importlib.util.spec_from_file_location('visualizer_large_runtime', snapshot / 'model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from threadpoolctl import threadpool_limits
    class Diagnostics(io.TextIOBase):
        def __init__(self):
            self.text = ''
            self.resource_failure = False
        def write(self, value):
            if any(marker in value for marker in ('MemoryError', 'bad_alloc', 'parser failed', 'fallback failed')):
                self.resource_failure = True
            self.text += value[:max(0, 8192 - len(self.text))]
            return len(value)

    diagnostics = Diagnostics()
    with threadpool_limits(limits=1), contextlib.redirect_stderr(diagnostics):
        adapter = module.RuntimeModel(artifacts_dir=str(snapshot / 'artifacts'))
        if adapter.twin_index is not None:
            raise ValueError('Raw-label structural-twin indices are unsupported')
        values = adapter.featurize(source)
    if diagnostics.resource_failure:
        raise ValueError('Full-source extraction failed within its resource budget; no prediction was produced. ' + diagnostics.text[:1000])
    if any(not math.isfinite(float(value)) for value in values.values()):
        raise ValueError('Model extractor returned a non-finite feature')
    return values


def _state(payload):
    from .selected_state import calculate_selected
    return calculate_selected(payload)


def _entry(connection, action, payload):
    try:
        if action == 'parse':
            from .circuits import parse
            result = parse(payload['qasm'])
        elif action == 'state':
            # Native libraries reserve thread stacks on import. Establish this
            # inside the spawned worker, before numpy/Qiskit/Aer and the memory
            # cap; do not depend on the server launcher's environment.
            import os
            for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                             'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS'):
                os.environ[variable] = '1'
            from .process_guard import enter_resource_job
            from .limits import STATE_MEMORY_BYTES
            resource_job = enter_resource_job(STATE_MEMORY_BYTES)
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):
                result = _state(payload)
        elif action == 'cloud_prepare':
            from .qbraid_service import prepare_program
            result = prepare_program(payload['record'], payload['step'], payload.get('step_kind', 'source'), payload.get('scope', 'checkpoint'), payload.get('acknowledge_initial_state', False))
        elif action == 'full_file_features':
            result = _full_file_features(payload)
        else:
            raise ValueError("Unknown worker action")
        connection.send({"ok": True, "result": result})
    except Exception as exc:
        connection.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


def run_isolated(action: str, payload: dict, timeout: float = 10):
    context = mp.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_entry, args=(sender, action, payload), daemon=True)
    started = time.perf_counter()
    process.start()
    sender.close()
    try:
        if not receiver.poll(max(0, timeout - (time.perf_counter() - started))):
            raise TimeoutError(f"{action.capitalize()} exceeded {timeout:g} seconds; the worker was terminated")
        try:
            message = receiver.recv()
        except EOFError as exc:
            raise ValueError("Computation worker exited without a result; the large-file memory budget may have been exceeded" if action == 'full_file_features' else "Computation worker exited without a result") from exc
        if not message['ok']:
            raise ValueError(message['error'])
        return message['result'], time.perf_counter() - started
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()
