"""Cloud behavior tests use only mocks; none submit a real qBraid job."""
import json
import importlib
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from visualizer.circuits import parse, load_qiskit
from visualizer.qbraid_api import create_qbraid_router
from visualizer.qbraid_service import CloudError, CloudService, SDKAdapter, device_summary, load_profiles, normalize_counts, prepare_program


SOURCE = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; creg c[2]; x q[0]; h q[1];'


class Remote:
    def __init__(self, name='one', org='org-a', balance=2000):
        self.name, self.org, self.credits = name, org, balance
        self.remote, self.submissions, self.calls = {}, 0, []
        self.cost, self.pricing_available, self.lose_response, self.error = 7.5, True, False, False

    def balance(self):
        self.calls.append('balance')
        return {'userId': self.name, 'organizationId': self.org, 'qbraidCredits': self.credits, 'awsCredits': 0, 'autoRecharge': 'disabled'}

    def device(self, device_id):
        self.calls.append('device')
        return {'qrn': device_id, 'name': 'Mock device', 'paradigm': 'gate_model', 'directAccess': True,
                'deviceType': 'SIMULATOR', 'status': 'ONLINE', 'numberQubits': 30,
                'minShots': 1, 'maxShots': 1000, 'queueDepth': 4, 'avgQueueTime': 2, 'runInputTypes': ['qasm2']}

    def devices(self):
        return [self.device('qbraid:qbraid:sim:mock')]

    def estimate(self, did, shots):
        self.calls.append('estimate')
        if self.error:
            raise RuntimeError('Credentials rejected: must never leak simulated-api-key')
        return {'deviceQrn': did, 'shots': shots, 'pricingAvailable': self.pricing_available, 'estimatedCost': self.cost, 'reason': None if self.pricing_available else 'Dynamic pricing unavailable'}

    def submit(self, did, program, shots, rid):
        self.submissions += 1
        self.remote[rid] = {'jobQrn': 'remote-' + rid, 'deviceQrn': did, 'shots': shots, 'tags': {'visualizer_request_id': rid}, 'status': 'QUEUED'}
        if self.lose_response:
            raise TimeoutError('lost response')
        return self.remote[rid]['jobQrn']

    def reconcile(self, rid):
        self.calls.append('reconcile')
        return [self.remote[rid]] if rid in self.remote else []

    def status(self, job_id):
        self.calls.append('status')
        return {**next(j for j in self.remote.values() if j['jobQrn'] == job_id), 'queuePosition': 2,
                'timeStamps': {'executionDuration': 2500}, 'cost': self.cost}

    def counts(self, job_id):
        return {'01': 52, '11': 48}


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    a, b = Remote(), Remote('two', 'org-b')
    monkeypatch.setenv('PROFILE_A', 'secret-a')
    monkeypatch.setenv('PROFILE_B', 'secret-b')
    monkeypatch.setenv('QBRAID_API_KEY', 'ambient-must-not-be-used')
    profiles = [{'id': 'a', 'label': 'First', 'api_key_env': 'PROFILE_A'}, {'id': 'b', 'label': 'Second', 'api_key_env': 'PROFILE_B'}]
    built = []
    def factory(key):
        built.append(key)
        return {'secret-a': a, 'secret-b': b}[key]
    service = CloudService(tmp_path, factory, profiles)
    prepared = prepare_program({'id': 'cid', 'source': SOURCE, 'parsed': parse(SOURCE)}, 2)
    selection = {'circuit_id': 'cid', 'step': 2, 'step_kind': 'source', 'profile_id': 'a', 'device_id': 'qbraid:qbraid:sim:mock', 'shots': 100}
    return service, a, b, prepared, selection, factory, profiles, built


def submission(service, selection, prepared, **extra):
    quote = service.quote(selection, prepared)
    return {**selection, 'request_id': str(uuid.uuid4()), 'quote_id': quote['id'], **extra}


def test_profiles_are_explicit_isolated_and_ambient_never_used(cloud, monkeypatch):
    service, a, b, prepared, selected, _, _, built = cloud
    assert service.profile_list()['profiles'][0]['wallet'] is None
    service.quote(selected, prepared)
    assert built == ['secret-a'] and not b.calls
    service.quote({**selected, 'profile_id': 'b'}, prepared)
    assert built == ['secret-a', 'secret-b']
    assert service.balance('a')['id'] != service.balance('b')['id']
    monkeypatch.delenv('PROFILE_A')
    fresh = CloudService(service.root, lambda _: pytest.fail('Ambient key used'), service.profiles)
    with pytest.raises(CloudError, match='credential'):
        fresh.balance('a')


def test_actual_wallet_identity_marks_shared_aliases(cloud):
    service, a, b, *_ = cloud
    b.name, b.org = a.name, a.org
    service.balance('a')
    second = service.balance('b')
    assert second['shared_with'] == ['a']
    assert service.balance('a')['shared_with'] == ['b']
    assert second['credits'] == 2000  # Never sum alias balances.


def test_quote_balance_and_device_shot_estimate_then_exhaustion_blocks_without_switch(cloud):
    service, a, b, prepared, selected, *_ = cloud
    request = submission(service, selected, prepared)
    quote = service._read('quotes', request['quote_id'])
    assert quote['estimated_cost'] == 7.5 and quote['estimated_remaining'] == 1992.5
    assert quote['device']['queue_depth'] == 4
    a.credits = 1
    with pytest.raises(CloudError) as error:
        service.submit(request, prepared)
    assert error.value.status == 402
    assert a.submissions == b.submissions == 0 and not b.calls


@pytest.mark.parametrize('qrn,provider,route', [
    ('aws:ionq:qpu:forte-1', 'IonQ', 'Amazon Braket'),
    ('ionq:ionq:qpu:forte-1', 'IonQ', 'IonQ'),
    ('openquantum:ionq:qpu:forte-1', 'IonQ', 'Open Quantum'),
    ('aws:aws:sim:sv1', 'Amazon Braket', 'Amazon Braket'),
])
def test_catalogue_distinguishes_device_provider_from_submission_route(qrn, provider, route):
    row = device_summary({**Remote().device(qrn), 'vendor': qrn.split(':')[0]})
    assert row['provider_name'] == provider and row['route_name'] == route


def test_catalogue_is_live_visible_metadata_not_execution_entitlement(cloud):
    service, a, b, *_ = cloud
    original = a.device
    rows = [
        original('aws:ionq:qpu:forte-1'),
        {**original('ionq:ionq:qpu:forte-1'), 'status': 'UNAVAILABLE'},
        {**original('aws:aws:sim:tn1'), 'status': 'RETIRED'},
        {**original('analog'), 'paradigm': 'analog'},
        {**original('indirect'), 'directAccess': False},
    ]
    a.devices = lambda: rows
    catalog = service.devices('a')
    assert [d['id'] for d in catalog['devices']] == ['aws:ionq:qpu:forte-1', 'ionq:ionq:qpu:forte-1']
    assert 'not confirmed account execution access' in catalog['note']
    rows.clear()
    refreshed = service.devices('a')
    assert refreshed['devices'] == [] and refreshed['checked_at'] >= catalog['checked_at']
    assert a.submissions == b.submissions == 0 and 'estimate' not in a.calls


def test_permission_denied_estimate_does_not_imply_catalogue_or_hardware_error(cloud):
    service, a, b, prepared, selected, *_ = cloud
    def denied(*_):
        error = RuntimeError('must-never-echo-provider-secret')
        error.response = SimpleNamespace(status_code=403)
        raise error
    a.estimate = denied
    with pytest.raises(CloudError, match='Catalogue visibility and ONLINE status') as error:
        service.quote(selected, prepared)
    assert 'provider-secret' not in str(error.value)
    assert a.submissions == b.submissions == 0 and not b.calls


@pytest.mark.parametrize('changed', [{'deviceQrn': 'other:device'}, {'shots': 999}])
def test_estimate_must_match_requested_device_and_shots(cloud, changed):
    service, a, _, prepared, selected, *_ = cloud
    original = a.estimate
    a.estimate = lambda did, shots: {**original(did, shots), **changed}
    with pytest.raises(CloudError, match='different device or shot count'):
        service.quote(selected, prepared)
    assert a.submissions == 0


def test_forte_api_credit_quote_is_preserved_without_claiming_execution_access(cloud):
    service, a, _, prepared, selected, *_ = cloud
    # Captured API rate is 30/task + 8/shot credits: a 100-shot estimate is 830.
    a.cost = 830
    quote = service.quote({**selected, 'device_id': 'aws:ionq:qpu:forte-1'}, prepared)
    assert quote['estimated_cost'] == 830 and quote['estimated_remaining'] == 1170
    assert quote['device']['provider_name'] == 'IonQ'
    assert 'not a guarantee of execution access' in quote['note']
    assert a.submissions == 0


def test_credentials_failure_never_falls_back_or_leaks_secret(cloud):
    service, a, b, prepared, selected, *_ = cloud
    a.error = True
    with pytest.raises(CloudError) as error:
        service.quote(selected, prepared)
    assert 'simulated-api-key' not in str(error.value)
    assert not b.calls


def test_unknown_and_external_zero_pricing_require_explicit_ack(cloud):
    service, a, _, prepared, selected, *_ = cloud
    selected = {**selected, 'device_id': 'openquantum:vendor:sim:mock'}
    a.cost = 0
    request = submission(service, selected, prepared)
    quote = service._read('quotes', request['quote_id'])
    assert quote['estimated_cost'] is None and quote['requires_unknown_ack']
    assert 'Open Quantum' in quote['billing_source'] and 'does not mean free' in quote['reason']
    with pytest.raises(CloudError, match='Acknowledge'):
        service.submit(request, prepared)
    job = service.submit({**request, 'acknowledge_unknown_pricing': True}, prepared)
    assert job['state'] == 'INITIALIZING' and a.submissions == 1


def test_quote_selection_hash_expiry_and_price_changes_are_bound(cloud):
    service, a, _, prepared, selected, *_ = cloud
    request = submission(service, selected, prepared)
    for key, changed in [('shots', 101), ('step', 1), ('profile_id', 'b')]:
        with pytest.raises(CloudError, match='does not match'):
            service.submit({**request, key: changed}, prepared)
    with pytest.raises(CloudError, match='does not match'):
        service.submit(request, {**prepared, 'program_hash': 'changed'})
    a.cost = 20
    with pytest.raises(CloudError, match='Pricing changed'):
        service.submit(request, prepared)
    assert a.submissions == 0


def test_duplicate_submit_and_lost_response_reconcile_once_even_after_restart(cloud):
    service, a, b, prepared, selected, factory, profiles, _ = cloud
    request = submission(service, selected, prepared)
    a.lose_response = True
    unknown = service.submit(request, prepared)
    assert unknown['state'] == 'OUTCOME_UNKNOWN' and a.submissions == 1
    reloaded = CloudService(service.root, factory, profiles)
    recovered = reloaded.submit(request, prepared)
    assert recovered['state'] == 'QUEUED' and recovered['remote_job_id']
    assert a.submissions == 1 and not b.calls
    assert recovered['program_hash'] == prepared['program_hash'] and recovered['program'] == prepared['program']
    assert recovered['profile_id'] == 'a' and recovered['checkpoint']['step'] == 2
    assert recovered['quote']['wallet']['user_id'] == a.name


def test_unresolved_outcome_never_resubmits(cloud):
    service, a, _, prepared, selected, *_ = cloud
    request = submission(service, selected, prepared)
    a.lose_response = True
    service.submit(request, prepared)
    a.remote.clear()
    for _ in range(3):
        assert service.submit(request, prepared)['state'] == 'OUTCOME_UNKNOWN'
    with pytest.raises(CloudError, match='unresolved request'):
        service.submit({**request, 'request_id': str(uuid.uuid4())}, prepared)
    assert a.submissions == 1


def test_results_use_original_profile_and_counts_compare_ideal_without_state_invention(cloud):
    service, a, b, prepared, selected, *_ = cloud
    request = submission(service, selected, prepared, ideal_probabilities={'01': .5, '11': .5})
    service.submit(request, prepared)
    service.balance('b')  # The user changes selected profile while the first job runs.
    a.remote[request['request_id']]['status'] = 'COMPLETED'
    result = service.refresh(request['request_id'])
    assert result['counts'] == {'01': 52, '11': 48}
    assert result['returned_shots'] == 100 and result['execution_seconds'] == 2.5
    assert result['total_variation_distance'] == pytest.approx(.02)
    assert 'status' not in b.calls and 'bloch' not in result and 'concurrence' not in result


def test_restart_profile_id_reuse_cannot_poll_with_different_wallet(cloud):
    service, a, b, prepared, selected, _, profiles, _ = cloud
    request = submission(service, selected, prepared)
    service.submit(request, prepared)
    restarted = CloudService(service.root, lambda _: b, profiles)
    with pytest.raises(CloudError, match='different wallet'):
        restarted.refresh(request['request_id'])
    assert 'balance' in b.calls and 'status' not in b.calls and 'reconcile' not in b.calls


def test_measurement_mapping_is_identity_and_counts_are_not_reversed():
    prepared = prepare_program({'source': SOURCE, 'parsed': parse(SOURCE)}, 2)
    qc = load_qiskit(prepared['program'])
    measurements = [(qc.find_bit(i.qubits[0]).index, qc.find_bit(i.clbits[0]).index) for i in qc.data if i.operation.name == 'measure']
    assert measurements == [(0, 0), (1, 1)]
    assert normalize_counts({'0b0 1': 6, '0x3': 4}, 2) == {'01': 6, '11': 4}
    with pytest.raises(CloudError):
        normalize_counts({'101': 4}, 2)


def test_nonunitary_prefix_rejected_before_submission():
    for suffix in [' measure q[0] -> c[0];', ' reset q[0];', ' if(c==1) x q[0];']:
        source = SOURCE + suffix
        with pytest.raises(CloudError, match='before measurement|classical inputs'):
            prepare_program({'source': source, 'parsed': parse(source)}, 3)


def test_cloud_qubit_limits_are_independent_of_local_exact_state_limit(cloud):
    service, _, _, _, selected, *_ = cloud
    source = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[20]; h q[0];'
    prepared = prepare_program({'source': source, 'parsed': parse(source)}, 1)
    assert service.quote({**selected, 'step': 1}, prepared)['qubits'] == 20
    with pytest.raises(CloudError, match='supports 30'):
        service.quote(selected, {**prepared, 'qubits': 31})


def test_http_authorization_precedes_quote_and_submit(cloud):
    service, a, b, _, selected, *_ = cloud
    app = FastAPI()
    def denied(cid):
        raise HTTPException(403, 'File no longer allowlisted')
    app.include_router(create_qbraid_router(denied, service.root, service))
    with TestClient(app) as client:
        assert client.post('/api/qbraid/quote', json=selected).status_code == 403
        assert client.post('/api/qbraid/submit', json={**selected, 'quote_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4())}).status_code == 403
    assert not a.calls and not b.calls


def test_no_profile_http_is_usable_and_credential_fields_not_returned(tmp_path, monkeypatch):
    monkeypatch.delenv('VISUALIZER_QBRAID_PROFILES', raising=False)
    assert load_profiles(tmp_path) == []
    app = FastAPI()
    app.include_router(create_qbraid_router(lambda _: None, tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/qbraid/profiles').json()['profiles'] == []
        assert client.get('/api/qbraid/jobs').json() == {'jobs': []}
        assert client.get('/api/qbraid/profiles/missing/balance').status_code == 404
    path = tmp_path / 'qbraid-profiles.json'
    path.write_text(json.dumps({'profiles': [{'id': 'a', 'label': 'First', 'api_key': 'must-not-be-allowed'}]}))
    with pytest.raises(CloudError, match='Invalid'):
        load_profiles(tmp_path)


def test_sdk_adapter_pins_explicit_key_tls_and_no_retry(monkeypatch):
    import qbraid_core
    import qbraid_core.services.runtime
    from qbraid.runtime import QbraidProvider
    from qbraid_core import QbraidSessionV1
    # Stub only the auth lookup, never perform a network request.
    monkeypatch.setattr(QbraidSessionV1, 'get_user_auth_metadata', lambda _: {'organizationId': 'test', 'organizationUserId': 'test'})
    monkeypatch.setattr(importlib.import_module('qbraid_core.client'), 'check_version', lambda _: None)
    monkeypatch.setenv('QBRAID_API_KEY', 'ambient-key')
    adapter = SDKAdapter('explicit-profile-key')
    session = adapter.client.session
    assert isinstance(adapter.provider, QbraidProvider)
    assert session.api_key == 'explicit-profile-key'
    assert session.headers['X-API-Key'] == 'explicit-profile-key'
    assert session.verify is True and session.trust_env is False
    retries = session.get_adapter('https://').max_retries
    assert retries.total == retries.connect == retries.read == retries.status == 0


def test_sdk_submission_serializes_one_program_with_uuid_tag_without_network(monkeypatch):
    from qbraid_core import QbraidSessionV1
    from qbraid_core.services.runtime.schemas import RuntimeDevice
    monkeypatch.setattr(QbraidSessionV1, 'get_user_auth_metadata', lambda _: {'organizationId': 'test', 'organizationUserId': 'test'})
    monkeypatch.setattr(importlib.import_module('qbraid_core.client'), 'check_version', lambda _: None)
    adapter = SDKAdapter('explicit-profile-key')
    metadata = RuntimeDevice.model_validate({**Remote().device('qbraid:qbraid:sim:mock'), 'vendor': 'qbraid'})
    monkeypatch.setattr(adapter.client, 'get_device', lambda _: metadata)
    submitted = []
    def capture(request):
        submitted.append(request.model_dump())
        return SimpleNamespace(jobQrn='mock-job')
    monkeypatch.setattr(adapter.client, 'create_job', capture)
    prepared = prepare_program({'source': SOURCE, 'parsed': parse(SOURCE)}, 2)
    rid = str(uuid.uuid4())
    assert adapter.submit(metadata.qrn, prepared['program'], 100, rid) == 'mock-job'
    assert len(submitted) == 1
    assert submitted[0]['tags'] == {'visualizer_request_id': rid}
    assert submitted[0]['shots'] == 100 and submitted[0]['program']['format'] == 'qasm2'
    assert 'measure' in submitted[0]['program']['data']


def test_adapter_preserves_missing_execution_duration_instead_of_inventing_queue_time(monkeypatch):
    adapter = object.__new__(SDKAdapter)
    raw = {'status': 'COMPLETED', 'timeStamps': {'createdAt': '2026-11-07T00:00:00Z', 'endedAt': '2026-11-07T01:00:00Z'}}
    adapter.client = SimpleNamespace(session=SimpleNamespace(get=lambda _: SimpleNamespace(json=lambda: {'success': True, 'data': raw})))
    assert adapter.status('job')['timeStamps'].get('executionDuration') is None


def test_killable_worker_prepares_source_prefix_without_cloud_calls():
    from visualizer.workers import run_isolated
    record = {'id': 'cid', 'source': SOURCE, 'parsed': parse(SOURCE)}
    result, elapsed = run_isolated('cloud_prepare', {'record': record, 'step': 1, 'step_kind': 'source'}, 10)
    assert result['checkpoint']['step'] == 1
    assert 'x q[0]' in result['program'] and 'h q[1]' not in result['program']
    assert elapsed < 10


def test_http_quote_submit_and_result_flow_mocked_remote(cloud):
    service, a, _, prepared, selected, *_ = cloud
    app = FastAPI()
    record = {'id': 'cid', 'source': SOURCE, 'parsed': parse(SOURCE)}
    authorized = []
    def resolver(cid):
        authorized.append(cid)
        return record
    app.include_router(create_qbraid_router(resolver, service.root, service))
    with TestClient(app) as client:
        response = client.post('/api/qbraid/quote', json=selected)
        assert response.status_code == 200, response.text
        rid = str(uuid.uuid4())
        response = client.post('/api/qbraid/submit', json={**selected, 'quote_id': response.json()['id'], 'request_id': rid, 'ideal_probabilities': {'01': .5, '11': .5}})
        assert response.status_code == 200, response.text
        assert a.submissions == 1
        a.remote[rid]['status'] = 'COMPLETED'
        response = client.get('/api/qbraid/jobs/' + rid)
        assert response.status_code == 200, response.text
        assert response.json()['counts'] == {'01': 52, '11': 48}
        assert len(client.get('/api/qbraid/jobs').json()['jobs']) == 1
        assert authorized == ['cid', 'cid']


def test_local_connection_validates_real_balance_without_estimate_or_submission(cloud):
    service, a, b, prepared, selected, *_ = cloud
    connected = service.connect('secret-a')
    pid = connected['profile']['id']
    assert pid.startswith('session_') and connected['wallet']['credits'] == 2000
    assert connected['profile']['session'] and connected['profile']['configured']
    assert a.calls == ['balance'] and a.submissions == b.submissions == 0
    assert not b.calls
    assert b'secret-a' not in service.db_path.read_bytes()
    assert 'secret-a' not in json.dumps(service.profile_list())
    # Catalogue kinds remain explicit, including physical hardware.
    original_device = a.device
    a.devices = lambda: [{**original_device('mock:qpu'), 'deviceType': 'QPU'}]
    assert service.devices(pid)['devices'][0]['kind'] == 'QPU'
    quote = service.quote({**selected, 'profile_id': pid, 'device_id': 'mock:qpu'}, prepared)
    assert quote['estimated_cost'] == 7.5 and quote['estimated_remaining'] == 1992.5
    assert a.submissions == 0


def test_disconnect_revokes_quote_pauses_jobs_and_reconnect_is_bound_to_original_wallet(cloud):
    service, a, b, prepared, selected, factory, profiles, _ = cloud
    pid = service.connect('secret-a')['profile']['id']
    selected = {**selected, 'profile_id': pid}
    request = submission(service, selected, prepared)
    service.submit(request, prepared)
    service.disconnect(pid)
    assert service._read('quotes', request['quote_id']) is None
    assert not next(p for p in service.profile_list()['profiles'] if p['id'] == pid)['configured']
    a.calls.clear()
    with pytest.raises(CloudError, match='disconnected or server restarted'):
        service.refresh(request['request_id'])
    assert 'status' not in a.calls and 'reconcile' not in a.calls
    restarted = CloudService(service.root, factory, profiles)
    with pytest.raises(CloudError, match='different wallet'):
        restarted.connect('secret-b', pid)
    assert pid not in restarted.clients and 'status' not in b.calls
    restored = restarted.connect('secret-a', pid)
    assert restored['profile']['id'] == pid
    assert restarted.refresh(request['request_id'])['state'] == 'QUEUED'
    with pytest.raises(CloudError, match='Disconnect'):
        restarted.connect('secret-a', pid)
    assert a.submissions == 1


@pytest.mark.parametrize('phase', ['constructor', 'balance'])
def test_connect_sdk_errors_even_cloud_errors_never_echo_key(tmp_path, phase):
    secret = 'fake-sensitive-connect-key'
    class BadRemote:
        def balance(self):
            raise CloudError(secret)
    def factory(token):
        if phase == 'constructor':
            raise CloudError(token)
        return BadRemote()
    service = CloudService(tmp_path, factory, [])
    app = FastAPI()
    app.include_router(create_qbraid_router(lambda _: None, tmp_path, service))
    with TestClient(app) as client:
        response = client.post('/api/qbraid/connect', json={'api_key': secret})
        assert response.status_code == 502 and secret not in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/api/qbraid/profiles').json()['profiles'] == []
    assert secret.encode() not in service.db_path.read_bytes()


@pytest.mark.parametrize('data', [
    {'api_key': 'private key'}, {'api_key': 'private-key-' * 500},
    {'api_key': ['private-key']}, {'api_key': {'private-key': True}},
    {'api_key': 'private-key', 'unexpected': 'private-key'},
    {'api_key': 'private-key', 'profile_id': ['private-key']},
])
def test_secret_bearing_validation_is_bounded_and_never_serializes_input(tmp_path, data):
    service = CloudService(tmp_path, lambda _: pytest.fail('Invalid input reached SDK'), [])
    app = FastAPI()
    app.include_router(create_qbraid_router(lambda _: None, tmp_path, service))
    with TestClient(app) as client:
        response = client.post('/api/qbraid/connect', json=data)
        assert response.status_code == 422 and 'private' not in response.text
        assert response.headers['cache-control'] == 'no-store'
        response = client.post('/api/qbraid/connect', content='{"api_key":"private-key"')
        assert response.status_code == 422 and 'private' not in response.text
        response = client.post('/api/qbraid/connect', content='private-key' * 1000)
        assert response.status_code == 422 and 'private' not in response.text


def test_session_connection_count_is_bounded(cloud):
    service, a, *_ = cloud
    for _ in range(8):
        service.connect('secret-a')
    with pytest.raises(CloudError) as exc:
        service.connect('secret-a')
    assert exc.value.status == 429 and len(a.calls) == 8


def test_restart_revokes_session_quote_and_named_profile_cannot_override_session_binding(cloud):
    service, a, _, prepared, selected, factory, profiles, _ = cloud
    pid = service.connect('secret-a')['profile']['id']
    selected = {**selected, 'profile_id': pid}
    request = submission(service, selected, prepared)
    restarted = CloudService(service.root, factory, profiles)
    assert restarted._read('quotes', request['quote_id']) is None
    restarted.connect('secret-a', pid)
    with pytest.raises(CloudError, match='does not match'):
        restarted.submit(request, prepared)
    with pytest.raises(CloudError, match='conflicts'):
        CloudService(service.root, factory, [{'id': pid, 'label': 'Reused', 'api_key_env': 'PROFILE_A'}])
    assert a.submissions == 0
