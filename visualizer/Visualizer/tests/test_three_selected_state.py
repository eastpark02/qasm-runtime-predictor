"""Three selected ideal marginals agree with full-state physics and API bounds."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from qiskit import QuantumCircuit, qasm2
from qiskit.quantum_info import Statevector, Pauli, partial_trace, entropy

from visualizer.app import Settings, StateCheckpoint, create_app
from visualizer.selected_state import calculate_selected
from visualizer.workers import run_isolated


@pytest.mark.parametrize('method', ['auto', 'stabilizer', 'statevector', 'matrix_product_state'])
@pytest.mark.parametrize('entangled', [False, True])
def test_three_unsorted_marginals_match_independent_physics_oracle(method, entangled):
    circuit = QuantumCircuit(5)
    circuit.x(4)
    circuit.h(1)
    circuit.s(1)  # q1's +Y direction tests phase and selected ordering.
    if entangled:
        circuit.h(0)
        circuit.cx(0, 2)
        circuit.cx(2, 4)
    selected = [4, 1, 2]
    expected = Statevector.from_instruction(circuit)
    result = calculate_selected({'qasm': qasm2.dumps(circuit), 'qubits': selected,
                                 'method': method, 'mps_cutoff': 0})
    assert result['selected_qubits'] == selected
    assert result['pair'] is None
    assert result['joint'] == [] and not result['global_joint_complete']
    assert 'ideal_probabilities' not in result
    for row, index in zip(result['qubits'], selected):
        reduced = partial_trace(expected, [i for i in range(5) if i != index])
        vector = [float(reduced.expectation_value(Pauli(axis)).real) for axis in 'XYZ']
        assert row['index'] == index
        assert row['bloch'] == pytest.approx(vector, abs=1e-8)
        assert row['length'] == pytest.approx(np.linalg.norm(vector), abs=1e-8)
        assert [row['p0'], row['p1']] == pytest.approx(reduced.probabilities(), abs=1e-8)
        assert row['entropy'] == pytest.approx(entropy(reduced), abs=1e-8)
        assert row['purity'] == pytest.approx(np.trace(reduced.data @ reduced.data).real, abs=1e-8)


@pytest.mark.parametrize('selection', [[0, 0, 1], [0, 1, 9], [0, 1, 2, 3]])
def test_invalid_three_qubit_selections_still_rejected(selection):
    with pytest.raises(ValueError, match='one to three different qubits'):
        calculate_selected({'qasm': 'OPENQASM 2.0; include "qelib1.inc"; qreg q[5];', 'qubits': selection})


def test_three_schema_cap_and_spawned_worker():
    assert StateCheckpoint(step=0, qubits=(2, 0, 1)).qubits == (2, 0, 1)
    with pytest.raises(ValidationError):
        StateCheckpoint(step=0, qubits=(0, 1, 2, 3))
    result, _ = run_isolated('state', {'qasm': 'OPENQASM 2.0; include "qelib1.inc"; qreg q[3]; h q[0]; cx q[0],q[1]; cx q[1],q[2];', 'qubits': [2, 0, 1]}, timeout=30)
    assert result['selected_qubits'] == [2, 0, 1]
    assert result['pair'] is None
    for row in result['qubits']:
        assert row['entropy'] == pytest.approx(1)
        assert row['purity'] == pytest.approx(.5)


class StubModel:
    info = {'available': False}


def test_three_api_cache_order_and_rejection(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path), model=StubModel())) as client:
        opened = client.post('/api/examples/ghz')
        assert opened.status_code == 200, opened.text
        circuit = opened.json()
        path = f'/api/circuits/{circuit["id"]}/state'
        request = {'step': 3, 'qubits': [2, 0, 1]}
        first = client.post(path, json=request)
        assert first.status_code == 200, first.text
        assert first.json()['selected_qubits'] == [2, 0, 1]
        assert not first.json()['cached']
        assert client.post(path, json=request).json()['cached']
        changed = client.post(path, json={**request, 'qubits': [1, 2, 0]})
        assert changed.status_code == 200, changed.text
        assert changed.json()['selected_qubits'] == [1, 2, 0]
        assert not changed.json()['cached']
        for selected in [[0, 0, 1], [0, 1, 99], [0, 1, 2, 3]]:
            assert client.post(path, json={**request, 'qubits': selected}).status_code == 422
