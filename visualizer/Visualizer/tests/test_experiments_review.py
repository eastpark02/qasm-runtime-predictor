"""Independent regression checks. Synthetic circuits/providers only; no account access."""
import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from visualizer.circuits import load_qiskit, parse
from visualizer.qbraid_api import Selection, create_qbraid_router
from visualizer.qbraid_service import CloudError, CloudService, SDKAdapter, prepare_program


HEADER = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; creg c[2]; '


def record(source):
    return {'id': 'checker-circuit', 'source': source, 'parsed': parse(source)}


def full(source, step=0):
    return prepare_program(record(source), step, scope='full')


def measurement_pairs(prepared):
    qc = load_qiskit(prepared['program'])
    return [(qc.find_bit(i.qubits[0]).index, qc.find_bit(i.clbits[0]).index)
            for i in qc.data if i.operation.name == 'measure']


def test_api_selection_defaults_to_full_independent_of_visualizer_zero():
    selected = Selection(circuit_id='checker-circuit', step=0, profile_id='a', device_id='fixture:sim')
    assert selected.scope == 'full'
    prepared = full(HEADER + 'x q[0]; h q[1];')
    qc = load_qiskit(prepared['program'])
    assert [i.operation.name for i in qc.data] == ['x', 'h', 'measure', 'measure']
    assert prepared['scope'] == 'full'
    assert measurement_pairs(prepared) == [(0, 0), (1, 1)]
    assert prepared['measurement_note']
    assert hashlib.sha256(prepared['program'].encode()).hexdigest() == prepared['program_hash']


@pytest.mark.parametrize('tail,pairs,width', [
    ('measure q[0] -> c[1]; measure q[1] -> c[0];', [(0, 1), (1, 0)], 2),
    ('measure q[1] -> c[0];', [(1, 0)], 2),
    ('measure q -> c;', [(0, 0), (1, 1)], 2),
])
def test_full_preserves_terminal_mapping_including_partial_measurements(tail, pairs, width):
    prepared = full(HEADER + 'x q[0]; h q[1]; ' + tail)
    assert measurement_pairs(prepared) == pairs
    assert prepared['classical_bits'] == width
    assert prepared['measurement_entries'] == [{'qubit': q, 'clbit': c} for q, c in pairs]
    assert prepared['measurement_note']


def test_mapping_across_registers_uses_flat_classical_bit_positions():
    source = 'OPENQASM 2.0; include "qelib1.inc"; qreg a[2]; qreg b[1]; creg low[1]; creg high[3]; x b[0]; measure b[0] -> high[2]; measure a[0] -> low[0];'
    prepared = full(source)
    assert prepared['classical_bits'] == 4
    assert measurement_pairs(prepared) == [(2, 3), (0, 0)]


@pytest.mark.parametrize('suffix', [
    'measure q[0] -> c[0]; x q[1];',
    'reset q[0];',
    'measure q[0] -> c[0]; if(c==1) x q[1];',
    'measure q[0] -> c[0]; measure q[1] -> c[0];',
])
def test_full_rejects_nonunitary_or_ambiguous_measurement_without_truncation(suffix):
    with pytest.raises(CloudError):
        full(HEADER + 'h q[0]; ' + suffix)


def test_explicit_checkpoint_can_precede_future_unsupported_ops_and_zero_requires_ack():
    r = record(HEADER + 'x q[0]; reset q[1];')
    prepared = prepare_program(r, 1, scope='checkpoint')
    assert [i.operation.name for i in load_qiskit(prepared['program']).data] == ['x', 'measure', 'measure']
    with pytest.raises(CloudError):
        prepare_program(r, 0, scope='checkpoint')
    initial = prepare_program(r, 0, scope='checkpoint', acknowledge_initial_state=True)
    assert [i.operation.name for i in load_qiskit(initial['program']).data] == ['measure', 'measure']


def test_uninspected_large_source_rejected_even_in_full_scope():
    r = {'id': 'large', 'source': HEADER + 'h q[0];', 'parsed': {'inspection_mode': 'full_source_only'}}
    with pytest.raises(CloudError, match='unavailable|inspected'):
        prepare_program(r, 0, scope='full')


class SyntheticRemote:
    """No SDK client is created by these service tests."""
    def __init__(self):
        self.submissions = []
        self.results = {'00': 50, '11': 50}
        self.details = {}
        self.state = 'COMPLETED'
        self.calls = []

    def balance(self):
        self.calls.append('balance')
        return {'userId': 'checker-user', 'organizationId': 'checker-org', 'qbraidCredits': 1000}

    def device(self, did):
        return {'qrn': did, 'name': 'Synthetic device', 'paradigm': 'gate_model', 'directAccess': True,
                'deviceType': 'SIMULATOR', 'status': 'ONLINE', 'numberQubits': 30,
                'minShots': 1, 'maxShots': 1000, 'runInputTypes': ['qasm2']}

    def estimate(self, did, shots):
        return {'deviceQrn': did, 'shots': shots, 'pricingAvailable': True, 'estimatedCost': 1}

    def submit(self, did, program, shots, rid, *args, **kwargs):
        self.submissions.append((did, program, shots, rid, args, kwargs))
        return 'synthetic-' + rid

    def status(self, job_id):
        self.calls.append('status')
        return {'status': self.state, 'cost': 1, 'timeStamps': {'createdAt': '2026-01-01T00:00:00Z',
                'endedAt': '2026-01-01T00:01:00Z', 'executionDuration': 2500}}

    def counts(self, job_id):
        self.calls.append('counts')
        return self.results

    def result_details(self, job_id):
        self.calls.append('result_details')
        return self.details

    def result_data(self, job_id):
        return {'counts': self.counts(job_id), 'details': self.details}

    def programs(self, job_id):
        return {'program': None, 'compiled_program': None}


@pytest.fixture
def synthetic_cloud(tmp_path, monkeypatch):
    remote = SyntheticRemote()
    monkeypatch.setenv('CHECKER_SYNTHETIC_ONLY', 'checker-fake-key-no-account')
    service = CloudService(tmp_path, lambda _: remote, [{'id': 'a', 'label': 'Synthetic only', 'api_key_env': 'CHECKER_SYNTHETIC_ONLY'}])
    return service, remote


def save_completed(service, remote, prepared, shots=100, device='qbraid:qbraid:sim:mock', **selection_extra):
    selection = {'circuit_id': 'checker-circuit', 'step': 0, 'step_kind': 'source', 'scope': 'full',
                 'acknowledge_initial_state': False, 'profile_id': 'a', 'device_id': device, 'shots': shots,
                 'runtime_options': {}, **selection_extra}
    quote = service.quote(selection, prepared)
    request = {**selection, 'quote_id': quote['id'], 'request_id': str(uuid.uuid4())}
    service.submit(request, prepared)
    return service.refresh(request['request_id'])


def test_swapped_partial_measurement_statistics_are_per_measured_qubit(synthetic_cloud):
    service, remote = synthetic_cloud
    remote.results = {'10': 75, '01': 25}
    prepared = full(HEADER + 'x q[0]; measure q[0] -> c[1]; measure q[1] -> c[0];')
    job = save_completed(service, remote, prepared)
    rows = {row['qubit']: row for row in job['measurement_statistics']['qubits']}
    assert rows[0]['p1'] == .75 and rows[1]['p1'] == .25
    assert rows[0]['z'] == -.5 and rows[1]['z'] == .5
    assert job['measurement_statistics']['pairs'][0]['zz'] == -1
    assert rows[0]['p1_standard_error'] == pytest.approx((.75*.25/100)**.5)
    assert job['execution_seconds'] == 2.5
    assert job['elapsed_seconds'] == 60
    assert 'concurrence' not in job and 'statevector' not in job


def test_partial_measurement_result_does_not_invent_unmeasured_qubit(synthetic_cloud):
    service, remote = synthetic_cloud
    remote.results = {'00': 75, '01': 25}
    job = save_completed(service, remote, full(HEADER + 'h q[1]; measure q[1] -> c[0];'))
    assert [r['qubit'] for r in job['measurement_statistics']['qubits']] == [1]
    assert job['measurement_statistics']['pairs'] == []


def test_quote_scope_and_program_mapping_are_bound_before_any_submit(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0];')
    selection = {'circuit_id': 'checker-circuit', 'step': 0, 'step_kind': 'source', 'scope': 'full',
                 'profile_id': 'a', 'device_id': 'qbraid:qbraid:sim:mock', 'shots': 100, 'runtime_options': {},
                 'acknowledge_initial_state': False}
    quote = service.quote(selection, prepared)
    request = {**selection, 'quote_id': quote['id'], 'request_id': str(uuid.uuid4())}
    with pytest.raises(CloudError):
        service.submit({**request, 'scope': 'checkpoint'}, prepared)
    assert remote.submissions == []


def test_compare_saved_jobs_computes_tvd_without_remote_calls_and_warns_shots(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0]; cx q[0],q[1];')
    first = save_completed(service, remote, prepared)
    remote.results = {'00': 150, '11': 50}
    second = save_completed(service, remote, prepared, shots=200)
    before = list(remote.calls)
    comparison = service.compare_jobs([first['request_id'], second['request_id']])
    assert remote.calls == before
    assert comparison['pairs'][0]['total_variation_distance'] == .25
    assert 'shot counts differ' in json.dumps(comparison['warnings']).lower()
    assert len(comparison['jobs']) == 2


def test_compare_rejects_missing_legacy_provenance_and_mapping_changes(synthetic_cloud):
    service, remote = synthetic_cloud
    first = save_completed(service, remote, full(HEADER + 'h q[0];'))
    second = save_completed(service, remote, full(HEADER + 'h q[0]; measure q[0] -> c[1]; measure q[1] -> c[0];'))
    with pytest.raises(CloudError):
        service.compare_jobs([first['request_id'], second['request_id']])
    legacy = copy.deepcopy(first)
    legacy['request_id'] = str(uuid.uuid4())
    legacy.pop('measurement_entries', None)
    legacy.pop('classical_bits', None)
    legacy.pop('measurement_fingerprint', None)
    with service.db() as db:
        db.execute('INSERT INTO jobs VALUES (?, ?)', (legacy['request_id'], json.dumps(legacy)))
    assert any(j['request_id'] == legacy['request_id'] for j in service.jobs())
    with pytest.raises(CloudError):
        service.compare_jobs([first['request_id'], legacy['request_id']])


@pytest.mark.parametrize('size', [0, 1, 5])
def test_compare_requires_two_to_four_unique_completed_jobs(synthetic_cloud, size):
    service, remote = synthetic_cloud
    job = save_completed(service, remote, full(HEADER + 'h q[0];'))
    with pytest.raises(CloudError):
        service.compare_jobs([job['request_id']] * size)


@pytest.mark.parametrize('did,options', [
    ('qbraid:qbraid:sim:gpu-tn', {'maxBondDim': 1}),
    ('qbraid:qbraid:sim:gpu-tn', {'method': 'exact', 'maxBondDim': 64}),
    ('qbraid:qbraid:sim:gpu-tn', {'method': []}),
    ('qbraid:qbraid:sim:gpu-sv', {'precision': {}}),
    ('qbraid:qbraid:sim:gpu-sv', {'seed': True}),
    ('qbraid:qbraid:sim:gpu-sv', {'seed': 2**32}),
    ('qbraid:qbraid:sim:gpu-sv', {'unknown': 1}),
    ('ionq:ionq:sim:simulator', {'noise': {'model': []}}),
    ('aws:ionq:qpu:forte-1', {'seed': 1}),
])
def test_invalid_options_are_rejected_explicitly_without_type_errors(did, options):
    from visualizer.qbraid_service import validate_runtime_options
    with pytest.raises(CloudError):
        validate_runtime_options(did, options)


def test_sanitized_provider_details_drop_secret_text_and_large_arrays():
    from visualizer.qbraid_service import bounded_details
    raw = {'simulationMethod': 'matrix_product_state', 'precision': 'double', 'maxBondDim': 64,
           'absoluteCutoff': 1e-5, 'relativeCutoff': 1e-5, 'numQubits': 2,
           'status_message': 'X-API-Key: fake-sensitive-key', 'statusMsg': 'fake-sensitive-key',
           'headers': {'Authorization': 'fake-sensitive-key'}, 'shots': ['fake-sensitive-key'] * 5000,
           'statevector': ['fake-sensitive-key'], 'nested': {'api_key': 'fake-sensitive-key'}}
    bounded = bounded_details(raw)
    assert bounded['simulationMethod'] == 'matrix_product_state'
    assert bounded['precision'] == 'double' and bounded['maxBondDim'] == 64
    assert 'fake-sensitive-key' not in json.dumps(bounded)
    assert len(json.dumps(bounded)) < 1000


def test_arbitrary_provider_status_cannot_leak_credentials(synthetic_cloud):
    service, remote = synthetic_cloud
    remote.state = 'FAILED X-API-Key: fake-sensitive-provider-key'
    try:
        job = save_completed(service, remote, full(HEADER + 'h q[0];'))
        assert 'fake-sensitive-provider-key' not in json.dumps(job)
    except CloudError as exc:
        assert 'fake-sensitive-provider-key' not in str(exc)
    assert 'fake-sensitive-provider-key' not in json.dumps(service.jobs())


def test_comparison_rejects_corrupt_persisted_counts_width(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0];')
    first = save_completed(service, remote, prepared)
    second = save_completed(service, remote, prepared)
    second['counts'] = {'000': 100}
    service._save_job(second)
    with pytest.raises(CloudError):
        service.compare_jobs([first['request_id'], second['request_id']])


def test_provider_optional_metadata_failure_does_not_invalidate_counts(synthetic_cloud):
    service, remote = synthetic_cloud
    remote.details = {'simulationMethod': 'matrix_product_state', 'precision': 'double', 'maxBondDim': 64,
                      'statusMsg': 'fake-sensitive-provider-key', 'api_key': 'fake-sensitive-provider-key'}
    def unsupported(job_id):
        raise RuntimeError('404 fake-sensitive-provider-key')
    remote.programs = unsupported
    job = save_completed(service, remote, full(HEADER + 'h q[0];'))
    assert job['counts'] == {'00': 50, '11': 50}
    assert job['result_details']['simulationMethod'] == 'matrix_product_state'
    assert 'fake-sensitive-provider-key' not in json.dumps(job)


@pytest.mark.parametrize('compiled', [None, 'raise', 'secret'])
def test_sdk_optional_program_fetch_is_bounded_and_failure_safe(monkeypatch, compiled):
    import qbraid.runtime.native
    secret = 'fake-sensitive-provider-key'
    class ProgramJob:
        def __init__(self, **kwargs):
            pass
        def program(self):
            return SimpleNamespace(format='qasm2', data=HEADER + 'h q[0];')
        def compiled_program(self):
            if compiled == 'raise':
                raise RuntimeError('404 ' + secret)
            return None if compiled is None else SimpleNamespace(format='qir.ll', data=secret)
    monkeypatch.setattr(qbraid.runtime.native, 'QbraidJob', ProgramJob)
    adapter = object.__new__(SDKAdapter)
    adapter.client = SimpleNamespace(session=SimpleNamespace(api_key=secret))
    output = adapter.programs('synthetic-only')
    assert output['submitted']['data'] == HEADER + 'h q[0];'
    assert 'compiled' not in output
    assert secret not in json.dumps(output)


def test_http_default_full_submits_all_gates_at_visualizer_zero_and_filters_wrong_ideal(synthetic_cloud, monkeypatch):
    import visualizer.qbraid_api as api_module
    service, remote = synthetic_cloud
    r = record(HEADER + 'x q[0]; h q[1];')
    def prepare_locally(action, payload, timeout):
        assert action == 'cloud_prepare'
        return prepare_program(payload['record'], payload['step'], payload['step_kind'], payload['scope'], payload['acknowledge_initial_state']), .01
    monkeypatch.setattr(api_module, 'run_isolated', prepare_locally)
    app = FastAPI()
    app.include_router(create_qbraid_router(lambda cid: r, service.root, service))
    selected = {'circuit_id': 'checker-circuit', 'step': 0, 'profile_id': 'a', 'device_id': 'qbraid:qbraid:sim:mock', 'shots': 100}
    with TestClient(app) as client:
        response = client.post('/api/qbraid/quote', json=selected)
        assert response.status_code == 200, response.text
        quote = response.json()
        assert quote['selection']['scope'] == 'full'
        request = {**selected, 'quote_id': quote['id'], 'request_id': str(uuid.uuid4()),
                   'ideal_probabilities': {'00': 1}, 'ideal_step': 0, 'ideal_step_kind': 'source', 'ideal_program_hash': 'wrong'}
        response = client.post('/api/qbraid/submit', json=request)
        # Wrong ideal provenance may be rejected, or safely omitted from the saved result.
        if response.status_code == 200:
            assert not response.json().get('ideal_probabilities')
        else:
            assert response.status_code in {409, 422}
            request = {k: v for k, v in request.items() if not k.startswith('ideal_')}
            response = client.post('/api/qbraid/submit', json=request)
            assert response.status_code == 200, response.text
        assert len(remote.submissions) == 1
        submitted = load_qiskit(remote.submissions[0][1])
        assert [i.operation.name for i in submitted.data] == ['x', 'h', 'measure', 'measure']


def test_http_matching_server_ideal_maps_to_preserved_partial_classical_wiring(synthetic_cloud, monkeypatch):
    import visualizer.qbraid_api as api_module
    service, remote = synthetic_cloud
    r = record(HEADER + 'x q[0]; h q[1]; measure q[0] -> c[1];')
    monkeypatch.setattr(api_module, 'run_isolated', lambda action, p, timeout:
                        (prepare_program(p['record'], p['step'], p['step_kind'], p['scope'], p['acknowledge_initial_state']), .01))
    ideal_calls = []
    def cached_ideal(cid, prepared):
        ideal_calls.append((cid, prepared['source_hash'], prepared['ideal_checkpoint']))
        assert prepared['ideal_checkpoint'] == {'step': 2, 'step_kind': 'source'}
        return {'01': .5, '11': .5}
    app = FastAPI()
    app.include_router(create_qbraid_router(lambda cid: r, service.root, service, get_ideal=cached_ideal))
    selected = {'circuit_id': 'checker-circuit', 'step': 0, 'profile_id': 'a', 'device_id': 'qbraid:qbraid:sim:mock', 'shots': 100}
    with TestClient(app) as client:
        quote = client.post('/api/qbraid/quote', json=selected).json()
        rid = str(uuid.uuid4())
        response = client.post('/api/qbraid/submit', json={**selected, 'quote_id': quote['id'], 'request_id': rid,
                                                        'ideal_probabilities': {'00': 1}})
        assert response.status_code == 200, response.text
        assert response.json()['ideal_probabilities'] == {'10': 1}
        remote.results = {'10': 100}
        result = client.get('/api/qbraid/jobs/' + rid).json()
        assert result['total_variation_distance'] == 0
        assert ideal_calls and all(call == ideal_calls[0] for call in ideal_calls)


def test_runtime_options_immutable_in_review_and_single_submit(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0];')
    selected = {'circuit_id': 'checker-circuit', 'step': 0, 'step_kind': 'source', 'scope': 'full',
                'acknowledge_initial_state': False, 'profile_id': 'a', 'device_id': 'qbraid:qbraid:sim:gpu-tn',
                'shots': 100, 'runtime_options': {'method': 'mps', 'seed': 0, 'maxBondDim': 64, 'precision': 'double'}}
    quote = service.quote(selected, prepared)
    request = {**selected, 'quote_id': quote['id'], 'request_id': str(uuid.uuid4())}
    with pytest.raises(CloudError):
        service.submit({**request, 'runtime_options': {'method': 'exact'}}, prepared)
    assert remote.submissions == []
    service.submit(request, prepared)
    service.submit(request, prepared)
    assert len(remote.submissions) == 1
    assert remote.submissions[0][4] == (selected['runtime_options'],)


def test_saved_comparison_normalizes_provider_bitstring_forms_before_tvd(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0];')
    first = save_completed(service, remote, prepared)
    second = save_completed(service, remote, prepared)
    second['counts'] = {'0b0 0': 50, '0x3': 50}
    service._save_job(second)
    result = service.compare_jobs([first['request_id'], second['request_id']])
    assert result['pairs'][0]['total_variation_distance'] == 0
    assert result['jobs'][1]['counts'] == first['counts']


@pytest.mark.parametrize('state', ['VALIDATING', 'CANCELLING', 'UNKNOWN'])
def test_documented_nonterminal_provider_statuses_are_pollable(synthetic_cloud, state):
    service, remote = synthetic_cloud
    remote.state = state
    result = save_completed(service, remote, full(HEADER + 'h q[0];'))
    assert result['state'] == state
    assert 'counts' not in remote.calls


def test_comparison_warns_different_returned_noise_under_provider_defaults(synthetic_cloud):
    service, remote = synthetic_cloud
    prepared = full(HEADER + 'h q[0];')
    first = save_completed(service, remote, prepared)
    second = save_completed(service, remote, prepared)
    first['returned_runtime_options'] = {'noise': {'model': 'ideal'}}
    second['returned_runtime_options'] = {'noise': {'model': 'harmony'}}
    service._save_job(first)
    service._save_job(second)
    result = service.compare_jobs([first['request_id'], second['request_id']])
    assert any('differ' in warning.lower() and ('returned' in warning.lower() or 'reported' in warning.lower()) for warning in result['warnings'])
