import math
import pytest

from visualizer.circuits import parse, prefix, load_qiskit
from visualizer.examples import EXAMPLES, HEADER
from visualizer.workers import _state, run_isolated


@pytest.mark.parametrize('key', EXAMPLES)
def test_examples_import_and_prefix_alignment(key):
    source = EXAMPLES[key]['qasm']
    parsed = parse(source)
    assert prefix(source, parsed, parsed['total_steps']) == source
    for i in range(parsed['total_steps'] + 1):
        qc = load_qiskit(prefix(source, parsed, i))
        expected = parsed['steps'][i - 1]['op_end'] if i else 0
        assert len(qc.data) == expected
        assert qc.num_qubits == parsed['num_qubits']
    for step in parsed['steps']:
        assert source[step['start']:step['end']] == step['source']
        assert source.count('\n', 0, step['start']) + 1 == step['line']


def test_broadcast_is_one_source_step_and_preserves_control_order():
    source = HEADER.replace('q[2]', 'q[3]') + 'h q;\ncx q[2],q[0];\nbarrier q;\n'
    parsed = parse(source)
    assert [s['instances'] for s in parsed['steps']] == [3, 1, 1]
    assert parsed['steps'][1]['qubits'] == [2, 0]
    assert len(load_qiskit(prefix(source, parsed, 1)).data) == 3


def test_comments_before_header_and_nested_angle_expression():
    source = '// circuit header\n/* introductory comment */\n' + HEADER + 'ry(sin(pi/2)) q[0];\n'
    parsed = parse(source)
    assert parsed['steps'][0]['angle_expression'] == 'sin(pi/2)'
    assert parsed['steps'][0]['parameters'][0]['value'] == pytest.approx(1)
    assert parsed['steps'][0]['line'] == 6


def test_declarations_after_first_operation_and_custom_body_survive_prefix():
    source = 'OPENQASM 2.0;\ninclude "qelib1.inc";\ngate pair(a) x,y { ry(a) x; cx x,y; }\nqreg q[2];\nh q[0];\ncreg c[2];\npair(pi/3) q[0],q[1];\nmeasure q -> c;\n'
    parsed = parse(source)
    assert 'pair(a)' in prefix(source, parsed, 1)
    assert 'creg c[2]' in prefix(source, parsed, 1)
    assert parsed['steps'][1]['body'].startswith('gate pair')
    assert len(load_qiskit(prefix(source, parsed, 2)).data) == 2


@pytest.mark.parametrize('bad', [HEADER + 'h q[9];', HEADER + 'h q[0]', HEADER + 'if(c==1) x q[0];', 'OPENQASM 2.0;\ninclude "../../secret.inc";\nqreg q[1];'])
def test_invalid_or_unsupported_qasm_is_explicit(bad):
    with pytest.raises(Exception):
        parse(bad)


def calculate(key, step, pair=(0, 1)):
    source = EXAMPLES[key]['qasm']
    return _state({'qasm': prefix(source, parse(source), step), 'pair': pair})


def test_product_states_have_unit_bloch_vectors_and_zero_entropy():
    result = calculate('product', 3)
    for qubit in result['qubits']:
        assert qubit['entropy'] == pytest.approx(0, abs=1e-10)
        assert qubit['length'] == pytest.approx(1)
    assert result['pair']['concurrence'] == pytest.approx(0, abs=1e-7)


def test_bell_entanglement_and_cx_undo():
    result = calculate('bell', 2)
    assert {j['bits']: j['probability'] for j in result['joint']} == pytest.approx({'00': .5, '11': .5})
    assert result['pair']['concurrence'] == pytest.approx(1)
    assert result['pair']['correlations'] == pytest.approx({'XX': 1, 'YY': -1, 'ZZ': 1})
    for q in result['qubits']:
        assert q['entropy'] == pytest.approx(1)
        assert q['length'] == pytest.approx(0, abs=1e-10)
    undone = calculate('bell', 3)
    assert undone['pair']['concurrence'] == pytest.approx(0, abs=1e-7)
    assert all(q['entropy'] == pytest.approx(0, abs=1e-10) for q in undone['qubits'])


def test_partial_entanglement_rotates_without_entropy_change():
    expected_entropy = -.75 * math.log2(.75) - .25 * math.log2(.25)
    vectors = [[0, 0, .5], [.5, 0, 0], [0, .5, 0]]
    for step, vector in zip([2, 3, 4], vectors):
        result = calculate('partial', step)
        assert result['qubits'][0]['bloch'] == pytest.approx(vector, abs=1e-10)
        assert result['qubits'][0]['entropy'] == pytest.approx(expected_entropy)
        assert result['pair']['concurrence'] == pytest.approx(math.sqrt(.75), abs=1e-7)


def test_ghz_pairwise_concurrence_does_not_capture_global_entanglement():
    result = calculate('ghz', 3)
    assert result['pair']['concurrence'] == pytest.approx(0, abs=1e-7)
    assert all(q['entropy'] == pytest.approx(1) for q in result['qubits'])


def test_custom_gate_state_and_measurement_boundary():
    assert calculate('custom', 1)['qubits'][0]['entropy'] == pytest.approx(.811278124459)
    with pytest.raises(ValueError, match='stops before'):
        calculate('custom', 2)


def test_state_limits_expanded_custom_gate_and_qubits():
    definitions = 'gate g0 a { x a; x a; }\n' + ''.join(f'gate g{i} a {{ g{i-1} a; g{i-1} a; }}\n' for i in range(1, 14))
    source = 'OPENQASM 2.0;\ninclude "qelib1.inc";\n' + definitions + 'qreg q[1];\ng13 q[0];'
    with pytest.raises(ValueError, match='10,000'):
        _state({'qasm': source})
    with pytest.raises(ValueError, match='12 qubits'):
        _state({'qasm': HEADER.replace('q[2]', 'q[13]') + 'h q[0];'})


def test_killable_worker_timeout_and_recovery():
    with pytest.raises(TimeoutError, match='terminated'):
        run_isolated('state', {'qasm': EXAMPLES['bell']['qasm']}, timeout=.001)
    result, elapsed = run_isolated('parse', {'qasm': EXAMPLES['partial']['qasm']}, timeout=10)
    assert result['total_steps'] == 4
    assert elapsed < 10
