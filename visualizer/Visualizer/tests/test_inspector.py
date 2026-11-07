"""Synthetic native expansion and true-prefix checks; no corpus reads."""
import hashlib
import json
from pathlib import Path
import numpy as np
import pytest
from fastapi.testclient import TestClient
from qiskit.quantum_info import Statevector
from visualizer.app import Settings, create_app
from visualizer.circuits import load_qiskit, prefix
from visualizer.inspector import expanded_prefix, native_parse, verified_inspector
from visualizer.workers import _state

ROOT = Path(__file__).resolve().parents[1]
HEADER = 'OPENQASM 2.0;\ninclude "qelib1.inc";\n'
SOURCE = HEADER + '''gate inner(a) x,y { ry(a/2) x; cx x,y; }
gate outer(a) x,y,z { inner(sin(a)) x,z; ccx x,y,z; }
qreg q[3];
creg c[3];
h q;
outer(pi/3) q[2],q[0],q[1];
measure q -> c;
'''

@pytest.fixture
def native():
    verified_inspector(ROOT)
    return lambda source: native_parse(source, ROOT)

def test_native_expansion_all_operands_params_and_origins(native):
    parsed = native(SOURCE)
    assert parsed['inspection_mode'] == 'native_expanded'
    assert parsed['total_steps'] == 3 and parsed['total_instructions'] == 9
    rows = parsed['instructions']
    assert [r['name'] for r in rows] == ['h'] * 3 + ['ry', 'cx', 'ccx'] + ['measure'] * 3
    assert rows[3]['qubits'] == [2]
    assert rows[3]['parameters'][0]['value'] == pytest.approx(np.sin(np.pi / 3) / 2)
    assert rows[4]['qubits'] == [2, 1] and rows[5]['qubits'] == [2, 0, 1]
    assert rows[3]['call_path'] == ['outer', 'inner']
    assert rows[5]['source_step'] == 2 and rows[5]['source'] == 'outer(pi/3) q[2],q[0],q[1];'
    assert rows[8]['clbits'] == [2]
    assert parsed['expanded_state_boundary'] == 7 and parsed['state_boundary'] == 3
    assert [s['instances'] for s in parsed['steps']] == [3, 1, 3]

def test_generated_prefix_matches_selected_execution_at_every_primitive(native):
    parsed = native(SOURCE)
    oracle = load_qiskit(HEADER + 'qreg q[3];\nh q[0];\nh q[1];\nh q[2];\n'
                         'ry(0.4330127018922193) q[2];\ncx q[2],q[1];\nccx q[2],q[0],q[1];')
    for step in range(7):
        program, kind = expanded_prefix(SOURCE, parsed, step)
        expected = oracle.copy_empty_like()
        for instruction in oracle.data[:step]:
            expected.append(instruction.operation, instruction.qubits)
        assert Statevector.from_instruction(load_qiskit(program)).equiv(Statevector.from_instruction(expected))
        assert _state({'qasm': program})['expanded_operations'] == step
        assert kind == ('generated_primitives' if step in (1, 2, 4, 5) else 'source_boundary')
    assert expanded_prefix(SOURCE, parsed, 9) == (SOURCE, 'original_full_source')
    assert expanded_prefix(SOURCE, parsed, 3)[0] == prefix(SOURCE, parsed, 1)
    assert expanded_prefix(SOURCE, parsed, 6)[0] == prefix(SOURCE, parsed, 2)

def test_qasm3_measurement_and_unicode_offsets(native):
    source = '\ufeff// π comment\nOPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[2] q; bit[2] c;\nrx(pi/7) q;\nc = measure q;\n'
    parsed = native(source)
    assert parsed['total_instructions'] == 4
    for row in parsed['instructions']:
        assert source[row['start']:row['end']] == row['source']
    assert [r['clbits'] for r in parsed['instructions'][2:]] == [[0], [1]]

@pytest.mark.parametrize('tail,reason', [
    ('h q[3];', 'Out-of-range'), ('bogus q[0];', 'Unsupported gate signature'),
    ('cx q,q[0];', 'Repeated gate qubits'), ('ry(foo) q[0];', 'Unsupported parameter'),
    ('ry(1e) q[0];', 'Malformed numeric'), ('ry(1/0) q[0];', 'Non-finite'),
    ('ctrl @ x q[0],q[1];', 'modified'), ('reset q[0],q[1];', 'Invalid state boundary'),
])
def test_strict_native_never_silently_approximates(native, tail, reason):
    with pytest.raises(ValueError, match=reason):
        native(HEADER + 'qreg q[3];\n' + tail)

def test_expansion_limit_and_recursion_are_rejected(native):
    definitions = 'gate g0 a { x a; x a; }\n' + ''.join(f'gate g{i} a {{ g{i-1} a; g{i-1} a; }}\n' for i in range(1, 17))
    with pytest.raises(ValueError, match='100,000'):
        native(HEADER + definitions + 'qreg q[1];\ng16 q[0];')
    with pytest.raises(ValueError, match='64 levels'):
        native(HEADER + 'gate loop a { loop a; }\nqreg q[1];\nloop q[0];')

@pytest.mark.parametrize('program,reason', [
    ('gate bad(a,a) x { ry(a) x; }\nqreg q[1];\nbad(1,2) q[0];', 'overlapping'),
    ('gate bad(a) a { ry(a) a; }\nqreg q[1];\nbad(1) q[0];', 'overlapping'),
    ('gate bad x { reset x; }\nqreg q[1];\nbad q[0];', 'Nonunitary'),
    ('gate bad x { later x; }\ngate later x { x x; }\nqreg q[1];\nbad q[0];', 'undeclared'),
    ('h q[0];\nqreg q[1];', 'Late declarations'),
    ('qreg q[1];\nh q[0];\ngate later x { x x; }', 'Late declarations'),
    ('qreg q[1];\ngphase(0.5);', 'QASM 3'),
])
def test_strict_scope_and_gate_body_validation(native, program, reason):
    with pytest.raises(ValueError, match=reason):
        native(HEADER + program)

def test_header_must_precede_includes(native):
    with pytest.raises(ValueError, match='Header must be first'):
        native('include "qelib1.inc";\nOPENQASM 2.0;\nqreg q[1];\nx q[0];')

@pytest.mark.parametrize('expression', ['sin(pi/7)', 'sqrt(2)/3', '-pi/9', '1.2345678901234567e-10'])
def test_roundtrip_generated_parameters_match_original_custom_gate(native, expression):
    source = HEADER + f'gate g(a1e) x {{ ry(a1e) x; rz(-a1e/3) x; }}\nqreg q[1];\ng({expression}) q[0];\nx q[0];'
    parsed = native(source)
    full = load_qiskit(source)
    expected = full.copy_empty_like()
    expected.ry(float(full.data[0].operation.params[0]), 0)
    generated, kind = expanded_prefix(source, parsed, 1)
    assert kind == 'generated_primitives'
    assert Statevector.from_instruction(load_qiskit(generated)).equiv(Statevector.from_instruction(expected))
    assert parsed['steps'][0]['parameters'][0]['value'] == pytest.approx(float(full.data[0].operation.params[0]))

def test_changed_expected_inspector_fingerprint_fails_closed(native):
    with pytest.raises(ValueError, match='changed since server startup'):
        native_parse(SOURCE, ROOT, 'not-the-session-fingerprint')

class PrefixModel:
    info = {'available': True, 'fingerprint': 'synthetic-prefix-model', 'thresholds': [16, 64, 256, 512, 1024]}
    def __init__(self):
        self.sources = []
    def features(self, source):
        self.sources.append(source)
        return {'st_ok': 1, 'st_truncated': 0, 'st_n_gates': 1}
    def derived(self, features, threshold):
        return {'selected_threshold': threshold}
    def predict(self, features, threshold):
        return 1.0
    def explain(self, features, threshold):
        return {'base_value': 0, 'output': 0, 'contribution_sum': 0, 'contributions': []}

def test_native_api_paging_cache_and_checkpoint_provenance(native, tmp_path):
    model = PrefixModel()
    app = create_app(Settings(root=ROOT), model=model)
    with TestClient(app) as client:
        from visualizer.access import AccessPolicy
        allowlist = tmp_path / 'synthetic-allowlist.json'
        allowlist.write_text(json.dumps({'scope': 'development_only', 'circuits': {
            hashlib.sha256(SOURCE.encode()).hexdigest(): {'origin': 'synthetic'}}}))
        app.state.policy = AccessPolicy(allowlist)
        uploaded = client.post('/api/circuits', content=SOURCE)
        assert uploaded.status_code == 200, uploaded.text
        summary = uploaded.json()
        assert summary['inspection_mode'] == 'native_expanded'
        cid = summary['id']
        page = client.get(f'/api/circuits/{cid}/instructions?offset=3&limit=2').json()
        assert page['step_kind'] == 'expanded'
        assert len(page['steps']) == 2 and page['total'] == 9
        assert client.get(f'/api/circuits/{cid}/instructions?limit=201').status_code == 422
        assert len(client.get(f'/api/circuits/{cid}/steps').json()['steps']) == 3
        assert client.get(f'/api/circuits/{cid}/overview?buckets=2').json()['buckets'] == [
            {'start': 1, 'end': 5, 'count': 5}, {'start': 6, 'end': 9, 'count': 4}]
        generated = client.post(f'/api/circuits/{cid}/features', json={'step': 4, 'step_kind': 'expanded'}).json()
        assert generated['prefix_kind'] == 'generated_primitives'
        assert generated['source_origin']['source_step'] == 2
        assert generated['provenance']['structural_feature_backend'] == 'native_cpp'
        assert len(load_qiskit(model.sources[-1]).data) == 4
        assert generated['derived_features'] == {'selected_threshold': 256}
        boundary = client.post(f'/api/circuits/{cid}/predict', json={'step': 3, 'step_kind': 'expanded'}).json()
        same = client.post(f'/api/circuits/{cid}/predict', json={'step': 1}).json()
        assert same['cached'] is True and same['prefix_hash'] == boundary['prefix_hash']
        assert same['step_kind'] == 'source' and same['source_origin']['source_step'] == 1
        assert client.post(f'/api/circuits/{cid}/state', json={'step': 7, 'step_kind': 'expanded'}).status_code == 422

def test_source_only_mode_does_not_claim_expansion(tmp_path):
    model = PrefixModel()
    with TestClient(create_app(Settings(root=tmp_path), model=model)) as client:
        uploaded = client.post('/api/examples/custom').json()
        assert uploaded['inspection_backend'] == 'python_source' and uploaded['fallback_reason']
        cid = uploaded['id']
        assert client.get(f'/api/circuits/{cid}/instructions').json()['step_kind'] == 'source'
        assert client.post(f'/api/circuits/{cid}/predict', json={'step': 1, 'step_kind': 'expanded'}).status_code == 422
        assert not model.sources
