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
    import numpy as np
    from qiskit.quantum_info import Statevector, partial_trace, entropy, concurrence
    from .circuits import load_qiskit, source_parts

    source_parts(payload['qasm'])  # Validate includes even for direct internal calls.
    circuit = load_qiskit(payload['qasm'])
    if circuit.num_qubits > 12:
        raise ValueError("Exact state calculation is limited to 12 qubits. Circuit inspection and runtime prediction remain available.")
    pair = payload.get('pair', [0, 1])
    if len(pair) != 2 or pair[0] == pair[1] or any(q < 0 or q >= circuit.num_qubits for q in pair):
        if circuit.num_qubits > 1:
            raise ValueError("Choose two different qubits from this circuit")
    if circuit.num_qubits == 0:
        raise ValueError("State calculation requires at least one qubit")
    from qiskit.circuit.library.standard_gates import get_standard_gate_name_mapping
    standard_names = set(get_standard_gate_name_mapping())
    count = 0
    def expanded(operation, depth=0):
        nonlocal count
        if depth > 64:
            raise ValueError("Custom-gate nesting exceeds 64 levels")
        if operation.name in {'barrier'}:
            return
        if operation.name in {'measure', 'reset', 'if_else', 'for_loop', 'while_loop', 'switch_case'}:
            raise ValueError("State calculation stops before measurement, reset, or classical branching")
        # Standard gates are leaves; only expand user-defined composite gates.
        if operation.name in standard_names:
            count += 1
        elif operation.definition is not None:
            for inst in operation.definition.data:
                expanded(inst.operation, depth + 1)
        else:
            count += 1
        if count > 10_000:
            raise ValueError("Exact state calculation is limited to 10,000 expanded operations")
    for inst in circuit.data:
        expanded(inst.operation)
    state = Statevector.from_int(0, 2 ** circuit.num_qubits)
    for inst in circuit.data:
        if inst.operation.name != 'barrier':
            state = state.evolve(inst.operation, qargs=[circuit.find_bit(q).index for q in inst.qubits])
    qubits = []
    for q in range(circuit.num_qubits):
        rho = partial_trace(state, [i for i in range(circuit.num_qubits) if i != q])
        data = rho.data
        vector = [float(2 * data[0, 1].real), float(-2 * data[0, 1].imag), float((data[0, 0] - data[1, 1]).real)]
        qubits.append({"index": q, "p0": float(data[0, 0].real), "p1": float(data[1, 1].real),
                       "bloch": vector, "length": float(np.linalg.norm(vector)), "entropy": float(entropy(rho))})
    probs = state.probabilities_dict()
    joint = sorted([{"bits": key, "probability": float(value), "amplitude_real": float(state.data[int(key, 2)].real),
                     "amplitude_imag": float(state.data[int(key, 2)].imag),
                     "phase": float(np.angle(state.data[int(key, 2)]))} for key, value in probs.items() if value > 1e-12],
                   key=lambda r: r['probability'], reverse=True)
    pair_result = None
    if circuit.num_qubits > 1:
        reduced = partial_trace(state, [i for i in range(circuit.num_qubits) if i not in pair])
        from qiskit.quantum_info import Pauli
        correlations = {basis: float(reduced.expectation_value(Pauli(basis)).real) for basis in ['XX', 'YY', 'ZZ']}
        pair_result = {"qubits": pair, "concurrence": float(concurrence(reduced)), "correlations": correlations}
    return {"qubits": qubits, "pair": pair_result, "joint": joint[:64], "joint_total": len(joint),
            "ideal_probabilities": {str(key): float(value) for key,value in probs.items()},
            "expanded_operations": count, "assumptions": "Ideal noiseless pure global state, initialized to all zeros. Bitstrings are q[n-1]…q[0]. Entropy measures each qubit versus the rest; pairwise concurrence does not capture all multipartite entanglement."}


def _entry(connection, action, payload):
    try:
        if action == 'parse':
            from .circuits import parse
            result = parse(payload['qasm'])
        elif action == 'state':
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
