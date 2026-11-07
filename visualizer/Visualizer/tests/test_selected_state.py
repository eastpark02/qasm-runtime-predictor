"""Synthetic reduced-state agreement and explicit resource/accuracy contracts."""
import json
import hashlib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from qiskit.quantum_info import Statevector, partial_trace, entropy, concurrence
from visualizer.circuits import load_qiskit
from visualizer.workers import _state
from visualizer.examples import EXAMPLES
from visualizer.app import create_app, Settings
from visualizer.selected_state import causal_circuit

HEADER = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[5]; '


def test_global_joint_preserves_circuit_definition_and_zero_qubit_phases():
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import GlobalPhaseGate
    inner = QuantumCircuit(1, name='inner_phase', global_phase=.29)
    inner.h(0)
    outer = QuantumCircuit(1, name='outer_phase', global_phase=.37)
    outer.append(inner.to_gate(), [0])
    circuit = QuantumCircuit(2, global_phase=.13)
    circuit.append(outer.to_gate(), [1])
    circuit.append(outer.to_gate(), [0])
    circuit.append(GlobalPhaseGate(.41), [])
    circuit.cx(0, 1)
    reduced, indices, _, _ = causal_circuit(circuit, [1], include_joint=True)
    assert indices == {0: 0, 1: 1}
    np.testing.assert_allclose(Statevector.from_instruction(reduced).data,
                               Statevector.from_instruction(circuit).data, atol=1e-12)
    # Selected reduced states intentionally need no global phase.
    selected, mapping, _, _ = causal_circuit(circuit, [1])
    original_rho = partial_trace(Statevector.from_instruction(circuit), [0])
    selected_rho = partial_trace(Statevector.from_instruction(selected),
                                 [q for q in range(selected.num_qubits) if q != mapping[1]])
    np.testing.assert_allclose(selected_rho.data, original_rho.data, atol=1e-12)


def test_qasm3_joint_amplitudes_preserve_global_and_custom_phase():
    source = ('OPENQASM 3.0; include "stdgates.inc"; '
              'gate phased a { gphase(pi/7); h a; } '
              'qubit[2] q; gphase(pi/3); phased q[0]; phased q[1]; cx q[0],q[1];')
    expected = Statevector.from_instruction(load_qiskit(source))
    result = _state({'qasm': source, 'qubits': [1], 'include_joint': True})
    actual = np.zeros(4, dtype=complex)
    for outcome in result['joint']:
        actual[int(outcome['bits'], 2)] = complex(outcome['amplitude_real'], outcome['amplitude_imag'])
    np.testing.assert_allclose(actual, expected.data, atol=1e-12)


@pytest.mark.parametrize('method', ['auto', 'statevector', 'matrix_product_state'])
@pytest.mark.parametrize('qubits', [[0], [4], [4, 1]])
def test_selected_agrees_with_full_qiskit_general_circuit(method, qubits):
    source = HEADER + 'ry(pi/7) q; h q[2]; cx q[0],q[4]; rz(pi/3) q[1]; cx q[4],q[1]; cy q[2],q[3]; cx q[1],q[3];'
    full = Statevector.from_instruction(load_qiskit(source))
    result = _state({'qasm': source, 'qubits': qubits, 'method': method, 'mps_cutoff': 0})
    for qubit in result['qubits']:
        q = qubit['index']
        rho = partial_trace(full, [i for i in range(5) if i != q])
        assert qubit['p0'] == pytest.approx(rho.data[0, 0].real, abs=1e-9)
        assert qubit['p1'] == pytest.approx(rho.data[1, 1].real, abs=1e-9)
        assert qubit['bloch'] == pytest.approx([2*rho.data[0,1].real, -2*rho.data[0,1].imag, (rho.data[0,0]-rho.data[1,1]).real], abs=1e-9)
        assert qubit['purity'] == pytest.approx(np.trace(rho.data @ rho.data).real, abs=1e-9)
        assert qubit['entropy'] == pytest.approx(entropy(rho), abs=1e-8)
    if len(qubits) == 2:
        pair = partial_trace(full, [i for i in range(5) if i not in qubits])
        assert result['pair']['concurrence'] == pytest.approx(concurrence(pair), abs=1e-7)
    assert result['global_joint_complete'] is False
    assert 'ideal_probabilities' not in result
    assert result['joint'] == []
    assert result['accuracy'] == ('approximate' if method == 'matrix_product_state' else 'exact')


def test_stabilizer_agrees_with_statevector_including_y_and_selected_order():
    source = HEADER + 'h q; s q[0]; cx q[0],q[2]; cy q[2],q[4]; s q[4]; cx q[4],q[1];'
    a = _state({'qasm': source, 'qubits': [4, 0], 'method': 'stabilizer'})
    b = _state({'qasm': source, 'qubits': [4, 0], 'method': 'statevector'})
    for first, second in zip(a['qubits'], b['qubits']):
        for key in ['p0', 'p1', 'purity', 'entropy', 'bloch']:
            assert first[key] == pytest.approx(second[key], abs=1e-9)
    assert a['pair']['correlations'] == pytest.approx(b['pair']['correlations'], abs=1e-9)
    assert a['pair']['concurrence'] == pytest.approx(b['pair']['concurrence'], abs=1e-7)


def test_wide_ghz_and_pruning_retain_entangled_inputs_without_global_vector():
    wide = EXAMPLES['wide_ghz']['qasm']
    result = _state({'qasm': wide, 'qubits': [31, 0]})
    assert result['method'] == 'stabilizer'
    assert result['causal_cone']['simulated_qubits'] == 32
    assert result['pair']['concurrence'] == pytest.approx(0, abs=1e-7)
    assert all(q['purity'] == pytest.approx(.5) and q['entropy'] == pytest.approx(1) for q in result['qubits'])
    # Later gates exclusively on traced environment cannot change q0's state.
    pruned = _state({'qasm': wide, 'qubits': [0]})
    assert pruned['causal_cone']['simulated_qubits'] == 2
    assert pruned['qubits'][0]['purity'] == pytest.approx(.5)
    independent = HEADER.replace('q[5]', 'q[200]') + 'ry(pi/7) q[199]; h q[0]; cx q[0],q[1];'
    pruned = _state({'qasm': independent, 'qubits': [199]})
    assert pruned['causal_cone']['qubits'] == [199]
    assert pruned['method'] == 'statevector'
    assert pruned['qubits'][0]['purity'] == pytest.approx(1)


def test_mps_auto_wide_routing_and_honest_truncation_settings():
    result = _state({'qasm': EXAMPLES['wide_mps']['qasm'], 'qubits': [23], 'mps_max_bond': 16, 'mps_cutoff': 1e-8})
    assert result['causal_cone']['simulated_qubits'] == 24
    assert result['method'] == 'matrix_product_state'
    assert result['accuracy'] == 'approximate'
    assert result['mps_settings']['max_bond_dimension'] == 16
    assert result['mps_settings']['truncation_threshold'] == 1e-8
    assert result['mps_settings']['error_bound'] is None


def test_method_and_joint_resource_preflight():
    source = EXAMPLES['wide_mps']['qasm']
    with pytest.raises(ValueError, match='not supported as an exact Clifford'):
        _state({'qasm': source, 'qubits': [23], 'method': 'stabilizer'})
    with pytest.raises(ValueError, match='budget is 22'):
        _state({'qasm': source, 'qubits': [23], 'method': 'statevector'})
    with pytest.raises(ValueError, match='limited to 12'):
        _state({'qasm': source, 'qubits': [23], 'include_joint': True})
    source = EXAMPLES['wide_mps']['qasm'].replace('q[24]', 'q[129]') + 'cx q[23],q[128];'
    # Force a full dependency chain exceeding the MPS width budget.
    source = HEADER.replace('q[5]', 'q[129]') + 'ry(pi/7) q; ' + ''.join(f'cx q[{i}],q[{i+1}];' for i in range(128))
    with pytest.raises(ValueError, match='budget is 128'):
        _state({'qasm': source, 'qubits': [128]})
    with pytest.raises(ValueError, match='bond'):
        _state({'qasm': HEADER, 'qubits': [0], 'mps_max_bond': 1024})


def test_only_explicit_global_joint_has_complete_cloud_reference():
    source = HEADER.replace('q[5]', 'q[7]') + 'h q;'
    selected = _state({'qasm': source, 'qubits': [0]})
    assert 'ideal_probabilities' not in selected
    global_result = _state({'qasm': source, 'qubits': [0], 'include_joint': True})
    assert global_result['global_joint_complete'] is True
    assert len(global_result['ideal_probabilities']) == 128
    assert sum(global_result['ideal_probabilities'].values()) == pytest.approx(1)
    assert len(global_result['joint']) == 64


class Stub:
    info = {'available': False}


def test_api_cache_identity_covers_selection_method_settings_and_joint(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path), model=Stub())) as client:
        cid = client.post('/api/examples/bell').json()['id']
        path = f'/api/circuits/{cid}/state'
        base = {'step': 2, 'qubits': [0]}
        first = client.post(path, json=base)
        assert first.status_code == 200, first.text
        assert not first.json()['cached']
        assert client.post(path, json=base).json()['cached']
        for change in [{'qubits': [1]}, {'qubits': [1, 0]}, {'method': 'statevector'}, {'mps_max_bond': 32}, {'mps_cutoff': 0}, {'include_joint': True}]:
            result = client.post(path, json={**base, **change})
            assert result.status_code == 200, result.text
            assert not result.json()['cached']
        assert client.post(path, json={**base, 'qubits': [0, 0]}).status_code == 422
        assert client.post(path, json={**base, 'mps_cutoff': 1}).status_code == 422
        limits = client.get('/api/health').json()['limits']
        assert limits['state_workers'] == 1 and limits['state_memory_bytes'] == 1024 ** 3
