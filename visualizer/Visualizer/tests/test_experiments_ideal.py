"""Complete ideal references reuse the computed state, even when the preview truncates."""
import pytest
from visualizer.workers import _state
from visualizer.qbraid_service import map_ideal


def test_dense_seven_qubit_state_retains_complete_reference_for_partial_swapped_mapping():
    source = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[7]; h q;'
    state = _state({'qasm': source, 'pair': [0, 1]})
    assert len(state['joint']) == 64 and state['joint_total'] == 128
    ideal = state['ideal_probabilities']
    assert len(ideal) == 128 and sum(ideal.values()) == pytest.approx(1)
    projected = map_ideal(ideal, [{'qubit': 6, 'clbit': 0}, {'qubit': 0, 'clbit': 2}], 4)
    assert projected == pytest.approx({'0000': .25, '0001': .25, '0100': .25, '0101': .25})
