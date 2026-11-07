import hashlib
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from visualizer.app import create_app, Settings
from visualizer.access import AccessPolicy
from visualizer.examples import EXAMPLES


class ModelStub:
    info = {'available': True, 'fingerprint': 'test-checkpoint', 'version': 'synthetic-tests'}
    def __init__(self):
        self.predictions = 0
        self.extractions = 0
    def features(self, source):
        self.extractions += 1
        return {'num_qubits': 2, 'n_ops': source.count(';')}
    def predict(self, features, threshold):
        self.predictions += 1
        return features['n_ops'] + threshold / 100
    def explain(self, features, threshold):
        return {'units': 'log10(seconds)', 'base_value': 0, 'output': 1, 'contributions': []}


@pytest.fixture
def client(tmp_path):
    stub = ModelStub()
    with TestClient(create_app(Settings(root=tmp_path), model=stub)) as client:
        yield client, stub


def opened(client, key='partial'):
    response = client.post(f'/api/examples/{key}')
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_navigation_and_features_do_not_predict_or_simulate(client):
    client, model = client
    cid = opened(client)
    for offset in [0, 1, 2]:
        assert client.get(f'/api/circuits/{cid}/steps?offset={offset}').status_code == 200
    assert model.predictions == 0
    assert model.extractions == 0
    response = client.post(f'/api/circuits/{cid}/features', json={'step': 2})
    assert response.status_code == 200
    assert model.predictions == 0
    assert model.extractions == 1


def test_unknown_content_and_renamed_files_rejected_before_computation(client, monkeypatch):
    client, model = client
    import visualizer.app as app_module
    def forbidden(*args, **kwargs):
        raise AssertionError('Unknown content reached a computation worker')
    monkeypatch.setattr(app_module, 'run_isolated', forbidden)
    source = EXAMPLES['bell']['qasm'] + '// changed content\n'
    response = client.post('/api/circuits?name=bell.qasm', content=source)
    assert response.status_code == 403
    assert model.predictions == model.extractions == 0


def test_allowlisted_upload_and_predictions_are_cached_by_threshold_and_step(client):
    client, model = client
    response = client.post('/api/circuits?name=renamed.qasm', content=EXAMPLES['partial']['qasm'])
    assert response.status_code == 200
    cid = response.json()['id']
    for step, threshold, expected_cached in [(1,256,False),(1,256,True),(2,256,False),(2,64,False)]:
        response = client.post(f'/api/circuits/{cid}/predict', json={'step':step, 'threshold':threshold})
        assert response.status_code == 200, response.text
        assert response.json()['cached'] is expected_cached
        assert response.json()['threshold'] == threshold
        assert response.json()['step'] == step
    assert model.predictions == 3
    assert model.extractions == 2
    assert client.post(f'/api/circuits/{cid}/predict', json={'step':999}).status_code == 422


def test_state_cache_boundaries_and_manual_pair_selection(client):
    client, _ = client
    cid = opened(client, 'ghz')
    a = client.post(f'/api/circuits/{cid}/state', json={'step':3, 'pair':[0,1]})
    assert a.status_code == 200, a.text
    assert a.json()['cached'] is False
    b = client.post(f'/api/circuits/{cid}/state', json={'step':3, 'pair':[0,1]})
    assert b.json()['cached'] is True
    c = client.post(f'/api/circuits/{cid}/state', json={'step':3, 'pair':[0,2]})
    assert c.status_code == 200, c.text
    assert c.json()['pair']['qubits'] == [0,2]
    assert c.json()['cached'] is False
    assert client.post(f'/api/circuits/{cid}/state', json={'step':3, 'pair':[0,0]}).status_code == 422
    cid = opened(client, 'custom')
    assert client.post(f'/api/circuits/{cid}/state', json={'step':2}).status_code == 422
    assert client.post(f'/api/circuits/{cid}/state', json={'step':1}).status_code == 200


def test_reset_boundary_and_single_qubit_state(tmp_path):
    source = 'OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[1];\nh q[0];\nreset q[0];\n'
    (tmp_path / 'approved-development.json').write_text(json.dumps({'scope':'development_only','circuits':{hashlib.sha256(source.encode()).hexdigest(): {'origin':'synthetic'}}}))
    with TestClient(create_app(Settings(root=tmp_path), model=ModelStub())) as client:
        response = client.post('/api/circuits', content=source)
        cid = response.json()['id']
        assert response.json()['state_boundary'] == 2
        result = client.post(f'/api/circuits/{cid}/state', json={'step':1})
        assert result.status_code == 200, result.text
        assert result.json()['pair'] is None
        assert client.post(f'/api/circuits/{cid}/state', json={'step':2}).status_code == 422


def test_origin_host_and_body_limits(client):
    client, _ = client
    assert client.get('/api/health', headers={'Host':'evil.example'}).status_code == 403
    assert client.post('/api/examples/bell', headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/circuits', headers={'Content-Length': str(256 * 1024 * 1024 + 1)}, content=b'').status_code == 413


def test_worker_timeout_surfaces_and_next_request_recovers(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path, state_timeout=.001), model=ModelStub())) as client:
        cid = opened(client)
        result = client.post(f'/api/circuits/{cid}/state', json={'step':2})
        assert result.status_code == 408, result.text
        assert client.get('/api/health').status_code == 200


def test_allowlist_preparation_excludes_audit_and_never_opens_corpus(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    from prepare_development import prepare
    path = tmp_path / 'development-record.json'
    output = tmp_path / 'approved-development.json'
    payload = {'operation':'fit_development','rows':[{'filename':'dev.qasm','fold':0,'group':'a'*64,'target_s':123}]}
    path.write_text(json.dumps(payload))
    original = Path.open
    opened_paths = []
    def watch(self, *args, **kwargs):
        opened_paths.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', watch)
    assert prepare(path, output) == 1
    assert set(opened_paths) <= {path, output}
    assert 'target_s' not in output.read_text()
    policy = AccessPolicy(output)
    assert 'a' * 64 in policy.entries
    payload['rows'][0]['fold'] = -1
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='audit'):
        prepare(path, output)


def test_global_importance_never_substitutes_training_rankings(client):
    client, _ = client
    response = client.get('/api/importance')
    assert response.json()['available'] is False
    assert 'No evaluation' in response.json()['note']
