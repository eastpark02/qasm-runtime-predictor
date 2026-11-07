"""Windowed native inspection above 100,000 statements; synthetic sources only."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import visualizer.app as app_module
from visualizer.app import Settings, create_app
from visualizer.circuits import prefix_at
from visualizer.inspector import native_parse, verified_inspector
from visualizer.qbraid_service import CloudError, preflight_program

ROOT = Path(__file__).resolve().parents[1]
WINDOW = 100_000
TOTAL = 250_000
PRELUDE = ('OPENQASM 2.0;\ninclude "qelib1.inc";\n// prelude comment π\n'
           'gate g(a) x,y { ry(a/2) x; cx x,y; }\nqreg q[2];\ncreg c[2];\n')
# (line text, statement text); comments and a non-ASCII character move byte
# offsets away from character offsets after the first window.
LINES = [('x q[0];', 'x q[0];'), ('h q[1];', 'h q[1];'), ('cx q[0],q[1];', 'cx q[0],q[1];'),
         ('rz(0.25) q[1];', 'rz(0.25) q[1];'), ('ry(pi/4) q[0]; // π', 'ry(pi/4) q[0];'),
         ('/* c */ swap q[0],q[1];', 'swap q[0],q[1];')]


def build(total=TOTAL):
    """Source plus each statement's text and byte/char start offsets."""
    parts, byte_starts, char_starts = [PRELUDE], [], []
    chars, size = len(PRELUDE), len(PRELUDE.encode())
    for i in range(total):
        if i == total - 1:
            line, text = 'measure q -> c;', 'measure q -> c;'
        elif i >= 2 * WINDOW and i % 7 == 0:
            line = text = 'g(0.5) q[1],q[0];'  # custom expansion, last window only
        elif i >= 2 * WINDOW and i % 11 == 0:
            line = text = 'h q;'  # register broadcast, last window only
        else:
            line, text = LINES[i % len(LINES)]
        lead = line.index(text)
        byte_starts.append(size + len(line[:lead].encode()))
        char_starts.append(chars + lead)
        parts.append(line + '\n')
        chars += len(line) + 1
        size += len(line.encode()) + 1
    return ''.join(parts), texts_of(total), byte_starts, char_starts


def texts_of(total):
    texts = []
    for i in range(total):
        if i == total - 1:
            texts.append('measure q -> c;')
        elif i >= 2 * WINDOW and i % 7 == 0:
            texts.append('g(0.5) q[1],q[0];')
        elif i >= 2 * WINDOW and i % 11 == 0:
            texts.append('h q;')
        else:
            texts.append(LINES[i % len(LINES)][1])
    return texts


@pytest.fixture(scope='module')
def big(tmp_path_factory):
    source, texts, byte_starts, char_starts = build()
    path = tmp_path_factory.mktemp('windowed') / 'big.qasm'
    path.write_bytes(source.encode())
    return {'source': source, 'texts': texts, 'bytes': byte_starts, 'chars': char_starts, 'path': path,
            'data': source.encode(), 'digest': hashlib.sha256(source.encode()).hexdigest()}


def run_cli(*args):
    executable, _ = verified_inspector(ROOT)
    result = subprocess.run([str(executable), *map(str, args)], capture_output=True, timeout=20)
    return result


# Captured from the pre-windowing qasm_inspect build on this exact source.
GOLDEN_SOURCE = ('OPENQASM 2.0;\ninclude "qelib1.inc";\ngate g(a) x,y { ry(a/2) x; cx x,y; }\nqreg q[2];\ncreg c[2];\n'
                 'h q;\ng(0.5) q[1],q[0];\nrz(0.25) q[1];\nmeasure q -> c;\n')
GOLDEN = ('{"num_qubits":2,"num_clbits":2,"qubit_labels":["q[0]","q[1]"],"clbit_labels":["c[0]","c[1]"],"source_widths":[2,1,1,2],'
          '"source_names":["h","g","rz","measure"],"source_parameters":[[],[0.5],[0.25],[]],"source_qubits":[[0,1],[1,0],[1],[0,1]],'
          '"source_clbits":[[],[],[],[0,1]],"instructions":['
          '{"name":"h","qubits":[0],"clbits":[],"parameter_values":[],"start":95,"end":99,"definition_start":95,"source_step":1,"call_path":[]},'
          '{"name":"h","qubits":[1],"clbits":[],"parameter_values":[],"start":95,"end":99,"definition_start":95,"source_step":1,"call_path":[]},'
          '{"name":"ry","qubits":[1],"clbits":[],"parameter_values":[0.25],"start":100,"end":117,"definition_start":52,"source_step":2,"call_path":["g"]},'
          '{"name":"cx","qubits":[1,0],"clbits":[],"parameter_values":[],"start":100,"end":117,"definition_start":63,"source_step":2,"call_path":["g"]},'
          '{"name":"rz","qubits":[1],"clbits":[],"parameter_values":[0.25],"start":118,"end":132,"definition_start":118,"source_step":3,"call_path":[]},'
          '{"name":"measure","qubits":[0],"clbits":[0],"parameter_values":[],"start":133,"end":148,"definition_start":133,"source_step":4,"call_path":[]},'
          '{"name":"measure","qubits":[1],"clbits":[1],"parameter_values":[],"start":133,"end":148,"definition_start":133,"source_step":4,"call_path":[]}]}')


def test_no_flag_cli_output_is_byte_identical(tmp_path):
    path = tmp_path / 'golden.qasm'
    path.write_bytes(GOLDEN_SOURCE.encode())
    result = run_cli(path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == GOLDEN.encode()


def test_no_flag_cli_still_refuses_more_than_100000_statements(big):
    result = run_cli(big['path'])
    assert result.returncode != 0 and b'Source inspection limit' in result.stderr
    with pytest.raises(ValueError, match='Source inspection limit'):
        native_parse(big['source'], ROOT)


def test_index_offsets_land_on_statement_starts(big):
    result = run_cli('--index', big['path'])
    assert result.returncode == 0, result.stderr
    index = json.loads(result.stdout)
    assert index['total_statements'] == TOTAL and index['window_size'] == WINDOW
    assert index['num_qubits'] == 2 and index['qubit_labels'] == ['q[0]', 'q[1]'] and index['clbit_labels'] == ['c[0]', 'c[1]']
    assert index['window_offsets'] == [big['bytes'][0], big['bytes'][WINDOW], big['bytes'][2 * WINDOW]]
    for k, offset in enumerate(index['window_offsets']):
        assert big['data'][offset:].startswith(big['texts'][k * WINDOW].encode())
    # Windows 0 and 1 contain only single-instance statements.
    assert index['window_op_offsets'] == [0, WINDOW, 2 * WINDOW]
    assert len(index['bucket_counts']) == 200 and sum(index['bucket_counts']) == TOTAL
    assert index['has_definitions'] is True and index['state_boundary'] == TOTAL


@pytest.mark.parametrize('k', [0, 1, 2])
def test_window_records_have_absolute_offsets_and_global_steps(big, k):
    offset = big['bytes'][k * WINDOW]
    result = run_cli('--window', big['path'], offset, k * WINDOW, WINDOW)
    assert result.returncode == 0, result.stderr
    window = json.loads(result.stdout)
    count = min(WINDOW, TOTAL - k * WINDOW)
    assert window['window'] == {'byte_offset': offset, 'base_step': k * WINDOW, 'count': WINDOW, 'statements_inspected': count}
    assert len(window['source_names']) == count
    steps = [r['source_step'] for r in window['instructions']]
    assert steps == sorted(steps) and set(steps) == set(range(k * WINDOW + 1, k * WINDOW + count + 1))
    for record in window['instructions']:
        index = record['source_step'] - 1
        assert record['start'] == big['bytes'][index]
        assert big['data'][record['start']:record['end']] == big['texts'][index].encode()
    if k == 2:
        assert any(r['call_path'] == ['g'] for r in window['instructions'])


def test_window_offset_must_be_a_statement_start(big):
    result = run_cli('--window', big['path'], big['bytes'][WINDOW] - 1, WINDOW, 10)
    assert result.returncode != 0 and b'statement start' in result.stderr
    result = run_cli('--window', big['path'], 3, 0, 10)
    assert result.returncode != 0 and b'precedes the first statement' in result.stderr


class CaptureModel:
    info = {'available': True, 'fingerprint': 'windowed-synthetic-model', 'thresholds': [256], 'stepper_available': False}

    def __init__(self):
        self.hashes = []

    def features(self, source):
        self.hashes.append(hashlib.sha256(source.encode()).hexdigest())
        return {'st_ok': 1, 'st_truncated': 0}

    def predict(self, features, threshold):
        return 1.0

    def explain(self, features, threshold):
        return {'contributions': []}


@pytest.fixture(scope='module')
def served(big):
    model = CaptureModel()
    app = create_app(Settings(root=ROOT), model=model)
    with TestClient(app) as client:
        app.state.policy.entries[big['digest']] = {'origin': 'synthetic'}
        response = client.post('/api/circuits?name=big.qasm', content=big['data'])
        assert response.status_code == 200, response.text
        yield client, app, model, response.json()


def test_upload_opens_windowed_mode(served, big):
    client, _, _, summary = served
    assert summary['inspection_mode'] == 'windowed' and summary['inspection_backend'] == 'app_owned_cpp_windowed'
    assert summary['full_source_only'] is False and summary['total_steps'] == TOTAL and summary['num_qubits'] == 2
    assert summary['total_instructions'] is None and summary['step_kind'] == 'source'
    assert summary['window_size'] == WINDOW and summary['state_boundary'] == TOTAL
    assert summary['windows'] == [{'index': k, 'start_step': k * WINDOW + 1, 'end_step': min(TOTAL, (k + 1) * WINDOW),
                                   'byte_offset': big['bytes'][k * WINDOW]} for k in range(3)]
    assert 'Source inspection limit' in summary['fallback_reason']
    assert any(w.startswith('Windowed native inspection') for w in summary['warnings'])
    limits = client.get('/api/health').json()['limits']
    assert limits['window_size'] == WINDOW and limits['windows_cached'] == 4


@pytest.mark.parametrize('offset', [0, 99_990, 100_000, 249_990])
def test_steps_resolve_windows_server_side(served, big, offset):
    client, _, _, summary = served
    page = client.get(f"/api/circuits/{summary['id']}/steps", params={'offset': offset, 'limit': 200})
    assert page.status_code == 200, page.text
    body = page.json()
    assert body['offset'] == offset and body['total'] == TOTAL
    rows = body['steps']
    assert [r['step'] for r in rows] == list(range(offset + 1, min(offset + 200, TOTAL) + 1))
    for row in rows:
        index = row['step'] - 1
        assert row['source_step'] == row['step']
        assert row['start'] == big['chars'][index] and big['source'][row['start']:row['end']] == big['texts'][index]
        assert row['source'] == big['texts'][index] and row['line'] == 7 + index
        assert row['expanded_start'] is None and row['expanded_end'] is None
        for key in ('name', 'qubits', 'clbits', 'parameters', 'instances', 'op_start', 'op_end', 'state_boundary', 'body'):
            assert key in row
    if offset < 2 * WINDOW:
        assert all(r['op_start'] == r['step'] - 1 and r['op_end'] == r['step'] for r in rows)
    if offset == 249_990:
        assert rows[-1]['name'] == 'measure' and rows[-1]['instances'] == 2 and rows[-1]['state_boundary'] is True


def test_overview_instructions_and_expanded_checkpoints(served):
    client, app, _, summary = served
    cid = summary['id']
    parsed = app.state.circuits.get(cid)['parsed']
    overview = client.get(f'/api/circuits/{cid}/overview', params={'buckets': 200}).json()
    assert overview['total'] == TOTAL and overview['step_kind'] == 'source'
    assert [b['count'] for b in overview['buckets']] == parsed['bucket_counts']
    default = client.get(f'/api/circuits/{cid}/overview').json()['buckets']
    assert len(default) == 100 and default[-1]['end'] == TOTAL and sum(b['count'] for b in default) == TOTAL
    response = client.get(f'/api/circuits/{cid}/instructions')
    assert response.status_code == 422 and 'windowed' in response.text
    response = client.post(f'/api/circuits/{cid}/features', json={'step': 5, 'step_kind': 'expanded'})
    assert response.status_code == 422 and 'windowed' in response.text
    assert client.post(f'/api/circuits/{cid}/features', json={'step': TOTAL + 1}).status_code == 422


def test_features_and_predict_use_prefix_through_selected_statement(served, big):
    client, app, model, summary = served
    cid = summary['id']
    index = 150_000 - 1
    end = big['chars'][index] + len(big['texts'][index])
    expected = hashlib.sha256((big['source'][:end].rstrip() + '\n').encode()).hexdigest()
    declarations = app.state.circuits.get(cid)['parsed']['declarations']
    assert expected == hashlib.sha256(prefix_at(big['source'], declarations, end).encode()).hexdigest()
    for action in ('features', 'predict'):
        result = client.post(f'/api/circuits/{cid}/{action}', json={'step': 150_000, 'threshold': 256})
        assert result.status_code == 200, result.text
        body = result.json()
        assert body['prefix_hash'] == expected and body['input_hash'] == big['digest']
        assert body['prefix_kind'] == 'source_boundary' and body['step_kind'] == 'source'
        assert body['source_origin'] == {'source': big['texts'][index], 'line': 7 + index, 'start': big['chars'][index],
                                         'end': end, 'source_step': 150_000}
    assert model.hashes[-1] == expected
    full = client.post(f'/api/circuits/{cid}/features', json={'step': TOTAL}).json()
    assert full['prefix_kind'] == 'original_full_source' and full['prefix_hash'] == big['digest']
    initial = client.post(f'/api/circuits/{cid}/features', json={'step': 0}).json()
    assert initial['source_origin'] is None
    # Declarations only (comments between them blanked, as prefix() does today).
    declarations_only = prefix_at(big['source'], declarations, 0)
    assert 'gate g(a)' in declarations_only and 'x q[0];' not in declarations_only
    assert initial['prefix_hash'] == hashlib.sha256(declarations_only.encode()).hexdigest()


def test_reauthorization_is_enforced_on_every_entry_point(served, big):
    client, app, _, summary = served
    cid = summary['id']
    entry = app.state.policy.entries.pop(big['digest'])
    try:
        assert client.get(f'/api/circuits/{cid}/steps').status_code == 403
        assert client.get(f'/api/circuits/{cid}/overview').status_code == 403
        for action in ('features', 'predict', 'explain', 'state', 'stepper'):
            assert client.post(f'/api/circuits/{cid}/{action}', json={'step': 150_000}).status_code == 403
    finally:
        app.state.policy.entries[big['digest']] = entry
    assert client.get(f'/api/circuits/{cid}/steps').status_code == 200


def test_cloud_preflight_rejects_windowed_mode(served):
    _, app, _, summary = served
    with pytest.raises(CloudError, match='windowed'):
        preflight_program(app.state.circuits.get(summary['id']), 10)


def test_window_cache_keeps_four_windows_per_circuit(monkeypatch):
    source = PRELUDE + 'x q[0];\n' * (4 * WINDOW + 1)
    calls = []
    original = app_module.native_window
    def counted(source, parsed, k, *args, **kwargs):
        calls.append(k)
        return original(source, parsed, k, *args, **kwargs)
    monkeypatch.setattr(app_module, 'native_window', counted)
    app = create_app(Settings(root=ROOT), model=CaptureModel())
    with TestClient(app) as client:
        digest = hashlib.sha256(source.encode()).hexdigest()
        app.state.policy.entries[digest] = {'origin': 'synthetic'}
        summary = client.post('/api/circuits', content=source.encode()).json()
        assert summary['inspection_mode'] == 'windowed' and len(summary['windows']) == 5
        def visit(k):
            response = client.get(f'/api/circuits/{digest}/steps', params={'offset': k * WINDOW, 'limit': 1})
            assert response.status_code == 200 and response.json()['steps'][0]['step'] == k * WINDOW + 1
        for k in (0, 1, 2, 3, 0):
            visit(k)
        assert calls == [0, 1, 2, 3]
        visit(4)  # evicts window 1, the least recently used
        visit(0)
        visit(1)
        assert calls == [0, 1, 2, 3, 4, 1]
        assert len(app.state.circuits.get(digest)['window_cache'].data) == 4


def test_window_exceeding_record_limit_fails_explicitly():
    source = PRELUDE + 'g(0.5) q[1],q[0];\n' * (WINDOW + 1)
    app = create_app(Settings(root=ROOT), model=CaptureModel())
    with TestClient(app) as client:
        digest = hashlib.sha256(source.encode()).hexdigest()
        app.state.policy.entries[digest] = {'origin': 'synthetic'}
        assert client.post('/api/circuits', content=source.encode()).json()['inspection_mode'] == 'windowed'
        response = client.get(f'/api/circuits/{digest}/steps')
        assert response.status_code == 422
        assert 'statements 1–100,000' in response.text and '100,000 instructions' in response.text
        tail = client.get(f'/api/circuits/{digest}/steps', params={'offset': WINDOW})
        assert tail.status_code == 200 and tail.json()['steps'][0]['name'] == 'g'
