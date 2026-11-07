"""Unlisted-upload switch: off by default, never edits the allowlist, revocable."""
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from visualizer.app import create_app, Settings
from visualizer.access import AccessPolicy, UNLISTED_WARNING, parse_flag
from visualizer.examples import EXAMPLES
from visualizer.qbraid_service import CloudError, preflight_program


class ModelStub:
    info = {'available': True, 'fingerprint': 'test-checkpoint', 'version': 'synthetic-tests'}
    def __init__(self):
        self.extractions = 0
    def features(self, source):
        self.extractions += 1
        return {'num_qubits': 2, 'n_ops': source.count(';')}
    def predict(self, features, threshold):
        return 1.0


def unlisted_source():
    # Synthetic, unique content: never on any allowlist.
    return f'OPENQASM 2.0;\ninclude "qelib1.inc";\n// unlisted-test {uuid.uuid4().hex}\nqreg q[2];\nh q[0];\ncx q[0],q[1];\n'


@pytest.fixture
def setup(tmp_path):
    manifest = tmp_path / 'approved-development.json'
    manifest.write_text(json.dumps({'scope': 'development_only', 'source': 'synthetic test manifest',
                                    'circuits': {'b' * 64: {'origin': 'development', 'name': 'dev.qasm'}}}))
    before = manifest.read_bytes()
    stub = ModelStub()
    with TestClient(create_app(Settings(root=tmp_path), model=stub)) as client:
        yield client, stub, manifest, before


def test_flag_parsing_accepts_documented_values_and_rejects_others(monkeypatch):
    for value in ['1', 'true', 'YES', ' on ']:
        assert parse_flag(value) is True
    for value in ['', '0', 'false', 'No', 'off']:
        assert parse_flag(value) is False
    with pytest.raises(ValueError, match='VISUALIZER_ALLOW_UNLISTED_UPLOADS'):
        parse_flag('maybe')
    monkeypatch.delenv('VISUALIZER_ALLOW_UNLISTED_UPLOADS', raising=False)
    assert Settings().allow_unlisted_uploads is False
    monkeypatch.setenv('VISUALIZER_ALLOW_UNLISTED_UPLOADS', 'on')
    assert Settings().allow_unlisted_uploads is True
    monkeypatch.setenv('VISUALIZER_ALLOW_UNLISTED_UPLOADS', '2')
    with pytest.raises(ValueError):
        Settings()


def test_policy_never_adds_entries_and_manifest_cannot_grant_unlisted(tmp_path):
    policy = AccessPolicy(tmp_path / 'missing.json')
    count = len(policy.entries)
    with pytest.raises(PermissionError):
        policy.authorize_digest('c' * 64)
    policy.allow_unlisted = True
    assert policy.authorize_digest('c' * 64) == ('c' * 64, {'origin': 'unlisted', 'name': None})
    assert len(policy.entries) == count and 'c' * 64 not in policy.entries
    manifest = tmp_path / 'bad.json'
    manifest.write_text(json.dumps({'scope': 'development_only', 'circuits': {'d' * 64: {'origin': 'unlisted'}}}))
    with pytest.raises(ValueError, match='Unapproved'):
        AccessPolicy(manifest)


def test_default_off_and_health_reports_scope(setup):
    client, _, _, _ = setup
    access = client.get('/api/access').json()
    assert access['scope'] == 'development_only' and access['allow_unlisted'] is False and access['default_from_env'] is False
    assert access['approved_files'] == len(EXAMPLES) + 1 and access['source'] == 'synthetic test manifest'
    assert 'OFF (the default' in access['note']
    assert client.get('/api/health').json()['access'] == access
    assert client.post('/api/circuits?name=x.qasm', content=unlisted_source()).status_code == 403


def test_toggle_validation_and_cross_origin_refusal(setup):
    client, _, _, _ = setup
    assert client.post('/api/access', json={'allow_unlisted': 'yes'}).status_code == 422
    assert client.post('/api/access', json={'allow_unlisted': True, 'extra': 1}).status_code == 422
    assert client.post('/api/access', json={'allow_unlisted': True}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/access').json()['allow_unlisted'] is False


def test_unlisted_upload_then_switch_off_revokes_cached_circuit(setup):
    client, stub, manifest, before = setup
    approved = client.get('/api/access').json()['approved_files']
    on = client.post('/api/access', json={'allow_unlisted': True}).json()
    assert on['scope'] == 'unrestricted' and on['allow_unlisted'] is True
    assert client.get('/api/health').json()['access']['scope'] == 'unrestricted'
    source = unlisted_source()
    response = client.post('/api/circuits?name=mine.qasm', content=source)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['origin'] == 'unlisted' and body['warnings'][0] == UNLISTED_WARNING
    cid = body['id']
    assert client.post(f'/api/circuits/{cid}/features', json={'step': 2}).status_code == 200
    assert client.post(f'/api/circuits/{cid}/features', json={'step': 2}).json()['cached'] is True
    assert stub.extractions == 1
    # Allowlisted examples are unaffected and keep their own origin and warnings.
    example = client.post('/api/examples/bell').json()
    assert example['origin'] == 'synthetic' and UNLISTED_WARNING not in example['warnings']
    off = client.post('/api/access', json={'allow_unlisted': False}).json()
    assert off['scope'] == 'development_only'
    # Cached results for the revoked circuit are not served.
    revoked = client.post(f'/api/circuits/{cid}/features', json={'step': 2})
    assert revoked.status_code == 403 and 'revoked' in revoked.json()['detail']
    for path in ['steps', 'instructions', 'overview']:
        assert client.get(f'/api/circuits/{cid}/{path}').status_code == 403
    for action in ['predict', 'explain', 'stepper', 'state']:
        assert client.post(f'/api/circuits/{cid}/{action}', json={'step': 2}).status_code == 403
    assert stub.extractions == 1
    assert client.post('/api/circuits?name=mine.qasm', content=source).status_code == 403
    assert client.post('/api/examples/bell').status_code == 200
    assert len(client.app.state.policy.entries) == approved
    assert manifest.read_bytes() == before


def test_cloud_execution_refused_for_unlisted_records():
    record = {'origin': 'unlisted', 'parsed': {'num_qubits': 2, 'total_steps': 2, 'state_boundary': None}}
    with pytest.raises(CloudError, match='unlisted') as caught:
        preflight_program(record, 1)
    assert caught.value.status == 403
    preflight_program({**record, 'origin': 'development'}, 1)


def test_switch_turned_off_mid_upload_rejects_and_caches_nothing(setup, monkeypatch):
    client, stub, manifest, before = setup
    client.post('/api/access', json={'allow_unlisted': True})
    policy = client.app.state.policy
    original = policy.authorize_digest
    def off_after_first_check(digest):
        result = original(digest)  # Authorized at upload time...
        policy.allow_unlisted = False  # ...then the switch is turned off while parsing.
        return result
    monkeypatch.setattr(policy, 'authorize_digest', off_after_first_check)
    source = unlisted_source()
    response = client.post('/api/circuits?name=race.qasm', content=source)
    assert response.status_code == 403 and 'revoked' in response.json()['detail']
    import hashlib
    assert client.app.state.circuits.get(hashlib.sha256(source.encode()).hexdigest()) is None
    from pathlib import Path
    assert list(Path(client.app.state.upload_directory.name).iterdir()) == []
    assert manifest.read_bytes() == before
