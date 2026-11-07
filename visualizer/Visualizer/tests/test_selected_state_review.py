"""Independent checker: physics oracle, pruning, budgets and cloud provenance."""
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from qiskit import QuantumCircuit, qasm2
from qiskit.quantum_info import Statevector, Pauli, partial_trace, entropy, concurrence

from visualizer.selected_state import calculate_selected, causal_circuit
from visualizer.app import create_app, Settings
from visualizer.workers import run_isolated


def source(circuit):
    return qasm2.dumps(circuit)


def check_oracle(circuit, selected, method):
    expected = Statevector.from_instruction(circuit)
    result = calculate_selected({'qasm': source(circuit), 'qubits': selected,
                                 'method': method, 'mps_cutoff': 0})
    assert result['selected_qubits'] == selected
    for actual, q in zip(result['qubits'], selected):
        rho = partial_trace(expected, [i for i in range(circuit.num_qubits) if i != q])
        # Pauli expectations independently test rho01 sign and all three axes.
        vector = [rho.expectation_value(Pauli(p)).real for p in 'XYZ']
        assert actual['index'] == q
        assert actual['bloch'] == pytest.approx(vector, abs=2e-8)
        assert actual['length'] == pytest.approx(np.linalg.norm(vector), abs=2e-8)
        assert [actual['p0'], actual['p1']] == pytest.approx(rho.probabilities(), abs=2e-8)
        assert actual['purity'] == pytest.approx(np.trace(rho.data @ rho.data).real, abs=2e-8)
        assert actual['entropy'] == pytest.approx(entropy(rho), abs=2e-8)
    if len(selected) == 2:
        rho = partial_trace(expected, [i for i in range(circuit.num_qubits) if i not in selected])
        assert result['pair']['concurrence'] == pytest.approx(concurrence(rho), abs=1e-7)
        assert result['pair']['correlations'] == pytest.approx({p: rho.expectation_value(Pauli(p)).real for p in ['XX', 'YY', 'ZZ']}, abs=2e-8)
    assert result['joint'] == [] and result['global_joint_complete'] is False
    assert 'ideal_probabilities' not in result
    return result


@pytest.mark.parametrize('method', ['auto', 'stabilizer', 'statevector', 'matrix_product_state'])
@pytest.mark.parametrize('case', ['asymmetric', 'bell', 'ghz', 'partial'])
def test_review_named_states_and_reversed_selection(method, case):
    q = QuantumCircuit(6)
    if case == 'asymmetric':
        q.x(0); q.h(5); q.s(5)  # q5 = +i, q0 = |1>: detects ordering and Y sign.
    elif case == 'bell':
        q.h(0); q.cx(0, 5)
    elif case == 'ghz':
        q.h(0); q.cx(0, 2); q.cx(2, 5)
    else:
        if method == 'stabilizer':
            pytest.skip('partial entanglement is non-Clifford')
        q.ry(np.pi / 3, 5); q.cx(5, 0)
    check_oracle(q, [5, 0], method)


@pytest.mark.parametrize('seed', range(8))
@pytest.mark.parametrize('method', ['stabilizer', 'statevector', 'matrix_product_state'])
def test_review_random_clifford_cross_method_oracle(seed, method):
    rng = np.random.default_rng(seed)
    q = QuantumCircuit(6)
    for _ in range(32):
        a, b = rng.choice(6, 2, replace=False).tolist()
        gate = rng.choice(['h', 's', 'sdg', 'cx', 'cy', 'cz', 'swap'])
        getattr(q, gate)(a, b) if gate in ['cx', 'cy', 'cz', 'swap'] else getattr(q, gate)(a)
    check_oracle(q, [5, 1], method)


@pytest.mark.parametrize('seed', range(6))
@pytest.mark.parametrize('method', ['auto', 'statevector', 'matrix_product_state'])
def test_review_nonclifford_and_custom_gate_oracle(seed, method):
    rng = np.random.default_rng(seed)
    sub = QuantumCircuit(3, name='custom')
    sub.ry(.317, 2); sub.cx(2, 0); sub.cp(.493, 0, 1); sub.swap(1, 2)
    q = QuantumCircuit(6)
    for _ in range(8):
        a, b = rng.choice(6, 2, replace=False).tolist()
        q.rx(float(rng.uniform(-3, 3)), a); q.ry(float(rng.uniform(-3, 3)), b); q.cx(a, b)
    q.append(sub.to_gate(), [5, 1, 0]); q.sdg(5)
    check_oracle(q, [5, 0], method)


def test_review_unrelated_nonclifford_is_pruned_but_entangled_ancestor_retained():
    q = QuantumCircuit(100)
    q.ry(.25, 99); q.h(0); q.cx(0, 1); q.cx(1, 2); q.t(2)
    result = calculate_selected({'qasm': source(q), 'qubits': [0]})
    assert result['method'] == 'stabilizer'
    assert result['causal_cone']['qubits'] == [0, 1]
    assert result['qubits'][0]['purity'] == pytest.approx(.5)
    assert result['qubits'][0]['entropy'] == pytest.approx(1)
    # A later operation brings the non-Clifford ancestor into the cone.
    q.cx(99, 0)
    result = calculate_selected({'qasm': source(q), 'qubits': [0]})
    assert result['method'] == 'statevector'
    assert result['causal_cone']['qubits'] == [0, 1, 99]


def test_review_joint_retains_global_phase_and_all_outcomes():
    q = QuantumCircuit(8); q.h(range(8)); q.global_phase = .371
    result = calculate_selected({'qasm': source(q), 'qubits': [7], 'include_joint': True})
    # QASM2 does not encode global phase; compare the serialized source oracle.
    expected = Statevector.from_instruction(QuantumCircuit.from_qasm_str(source(q)))
    assert result['global_joint_complete'] and result['accuracy'] == 'exact'
    assert len(result['ideal_probabilities']) == 256
    assert len(result['joint']) == 64 and result['joint_total'] == 256
    for row in result['joint']:
        assert complex(row['amplitude_real'], row['amplitude_imag']) == pytest.approx(expected.data[int(row['bits'], 2)], abs=1e-10)


def test_review_joint_custom_nested_and_zero_qubit_global_phases():
    inner = QuantumCircuit(1, global_phase=.241)
    inner.ry(.3, 0)
    outer = QuantumCircuit(1, global_phase=.147)
    outer.append(inner.to_gate(), [0])
    original = QuantumCircuit(2, global_phase=.043)
    original.h(0); original.append(outer.to_gate(), [1])
    phase = QuantumCircuit(0, global_phase=.27)
    original.append(phase.to_gate(), [])
    flattened, _, _, _ = causal_circuit(original, [1], include_joint=True)
    assert Statevector.from_instruction(flattened).data == pytest.approx(Statevector.from_instruction(original).data, abs=1e-12)


def test_review_operation_and_width_caps_preflight():
    q = QuantumCircuit(513)
    q.h(0)
    for i in range(512): q.cx(i, i + 1)
    with pytest.raises(ValueError, match='512'):
        calculate_selected({'qasm': source(q), 'qubits': [512]})
    q = QuantumCircuit(1)
    for _ in range(10001): q.x(0)
    with pytest.raises(ValueError, match='10,000'):
        calculate_selected({'qasm': source(q), 'qubits': [0]})


def test_review_wide_states_against_analytic_expectations():
    from visualizer.examples import EXAMPLES
    ghz = calculate_selected({'qasm': EXAMPLES['wide_ghz']['qasm'], 'qubits': [31, 0]})
    assert ghz['method'] == 'stabilizer' and ghz['accuracy'] == 'exact'
    assert ghz['causal_cone']['simulated_qubits'] == 32
    assert ghz['pair']['concurrence'] == pytest.approx(0)
    assert ghz['pair']['correlations'] == pytest.approx({'XX': 0, 'YY': 0, 'ZZ': 1})
    for row in ghz['qubits']:
        assert row['bloch'] == pytest.approx([0, 0, 0])
        assert row['entropy'] == pytest.approx(1)
    mps = calculate_selected({'qasm': EXAMPLES['wide_mps']['qasm'], 'qubits': [23]})
    vector = [np.sin(np.pi / 7), 0, np.cos(np.pi / 7) ** 24]
    length = np.linalg.norm(vector)
    assert mps['qubits'][0]['bloch'] == pytest.approx(vector, abs=1e-7)
    assert mps['qubits'][0]['purity'] == pytest.approx((1 + length ** 2) / 2, abs=1e-7)
    assert mps['method'] == 'matrix_product_state' and mps['accuracy'] == 'approximate'
    assert mps['mps_settings']['error_bound'] is None
    assert all('ideal_probabilities' not in row for row in [ghz, mps])


class StubModel:
    info = {'available': False}


def test_review_cloud_reference_is_explicit_complete_and_checkpoint_bound(tmp_path, monkeypatch):
    import visualizer.qbraid_api as cloud
    captured = {}
    def fake_router(*args, **kwargs):
        captured.update(kwargs)
        return APIRouter()
    monkeypatch.setattr(cloud, 'create_qbraid_router', fake_router)
    with TestClient(create_app(Settings(root=tmp_path), model=StubModel())) as client:
        opened = client.post('/api/examples/bell')
        assert opened.status_code == 200, opened.text
        circuit = opened.json()
        cid = circuit['id']
        record = client.app.state.circuits.get(cid)
        prepared = {'source_hash': hashlib.sha256(record['source'].encode()).hexdigest(), 'ideal_checkpoint': {'step': 2, 'step_kind': 'source'}}
        path = f'/api/circuits/{cid}/state'
        assert client.post(path, json={'step': 2, 'qubits': [1]}).status_code == 200
        assert captured['get_ideal'](cid, prepared) is None
        reply = client.post(path, json={'step': 2, 'qubits': [1], 'include_joint': True})
        assert reply.status_code == 200, reply.text
        assert captured['get_ideal'](cid, prepared) == pytest.approx({'00': .5, '01': 0, '10': 0, '11': .5})
        assert captured['get_ideal'](cid, {**prepared, 'source_hash': 'wrong'}) is None
        assert captured['get_ideal'](cid, {**prepared, 'ideal_checkpoint': {'step': 1, 'step_kind': 'source'}}) is None
        assert captured['get_ideal'](cid, {**prepared, 'ideal_checkpoint': {'step': 2, 'step_kind': 'expanded'}}) is None


def test_review_state_concurrency_and_timeout_release(tmp_path, monkeypatch):
    import visualizer.app as module
    entered, release = threading.Event(), threading.Event()
    actual = module.run_isolated
    def slow(action, payload, timeout):
        if action == 'state':
            entered.set()
            assert release.wait(10)
            raise TimeoutError('checker synthetic state timeout')
        return actual(action, payload, timeout)
    monkeypatch.setattr(module, 'run_isolated', slow)
    with TestClient(create_app(Settings(root=tmp_path), model=StubModel())) as client:
        cid = client.post('/api/examples/bell').json()['id']
        path = f'/api/circuits/{cid}/state'
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(client.post, path, json={'step': 2, 'qubits': [0]})
            assert entered.wait(10)
            assert client.post(path, json={'step': 2, 'qubits': [1]}).status_code == 429
            release.set()
            assert pending.result().status_code == 408
        monkeypatch.setattr(module, 'run_isolated', actual)
        recovered = client.post(path, json={'step': 2, 'qubits': [0]})
        assert recovered.status_code == 200, recovered.text


def test_review_real_spawn_timeout_leaves_no_worker():
    import psutil
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    with pytest.raises(TimeoutError, match='terminated'):
        run_isolated('state', {'qasm': source(QuantumCircuit(1)), 'qubits': [0]}, timeout=.001)
    assert {p.pid for p in psutil.Process().children(recursive=True)} <= before


@pytest.mark.parametrize('method', ['auto', 'matrix_product_state'])
def test_review_real_spawn_state_needs_no_launcher_thread_settings(monkeypatch, method):
    for key in ['OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS']:
        monkeypatch.setenv(key, '64')
    q = QuantumCircuit(4); q.h(3); q.s(3); q.cx(3, 1)
    result, elapsed = run_isolated('state', {'qasm': source(q), 'qubits': [3, 1], 'method': method}, timeout=30)
    assert elapsed < 30
    assert result['pair']['concurrence'] == pytest.approx(1, abs=1e-7)
    assert all(row['purity'] == pytest.approx(.5) for row in result['qubits'])
