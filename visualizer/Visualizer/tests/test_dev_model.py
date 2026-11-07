"""Inference-only regression checks. Every circuit below is bundled or synthetic."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from visualizer.app import Settings, create_app
from visualizer.circuits import parse, prefix
from visualizer.dev_model import DevModelService, FILES, THRESHOLDS
from visualizer.examples import EXAMPLES, HEADER
from visualizer.stepper import parse_trace

ROOT = Path(__file__).resolve().parents[1]
PREDICTOR = Path(os.environ.get('VISUALIZER_PREDICTOR', ROOT.parent / 'quantum-runtime-prediction-model' / 'qasm-runtime-predictor'))


@pytest.fixture(scope='module')
def service(tmp_path_factory):
    local = tmp_path_factory.mktemp('dev-snapshot')
    (local / 'runtime').mkdir()
    for name in ('qasm_step.exe', 'build.json'):
        shutil.copyfile(ROOT / '.local/runtime' / name, local / 'runtime' / name)
    # Explicitly exercise PowerShell 5's UTF-8 BOM metadata.
    build = local / 'runtime/build.json'
    build.write_text(build.read_text(encoding='utf-8-sig'), encoding='utf-8-sig')
    service = DevModelService(PREDICTOR, local, ROOT / 'runtime-checkpoint.json')
    assert service.info['available'], service.info
    yield service


def test_pinned_blend_provenance_and_fingerprints(service):
    info = service.info
    assert info['commit'] == 'ffaa06e3a6151a68317ce35527d5a707ac678b8d'
    assert info['fingerprint'] == 'becf030147191a76bb74b3fd4384b89f296c1fb344148828082f48c1dca517e5'
    assert info['config'] == {'kind': 'HistGradientBoosting blend', 'members': 4}
    assert info['training_scope'] == 'upstream_all_rows'
    assert (info['rows_fitted'], info['circuits_fitted'], info['raw_label_index']) == (2648, 532, False)
    assert service.adapter.twin_index is None
    assert set(info['source_fingerprints']) == set(FILES)
    assert all(type(m).__name__ == 'HistGradientBoostingRegressor' for _, m in service.adapter.members)
    snapshot = Path(info['snapshot'])
    for name, digest in info['source_fingerprints'].items():
        path = snapshot / ('artifacts/model.pkl' if name == 'artifacts/model.pkl' else Path(name).name)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(service.binary.read_bytes()).hexdigest() == info['stepper_fingerprint']


@pytest.mark.parametrize('key', EXAMPLES)
def test_full_source_and_upstream_adapter_exact_agreement(service, key):
    source = EXAMPLES[key]['qasm']
    parsed = parse(source)
    assert prefix(source, parsed, parsed['total_steps']) == source
    # A separate instance of the exact unmodified upstream adapter from the
    # verified snapshot; never run upstream training/evaluation modules.
    with threadpool_limits(limits=1):
        direct = service.module.RuntimeModel(artifacts_dir=str(Path(service.info['snapshot']) / 'artifacts'))
        expected = direct.featurize(source)
        actual = service.features(source)
        assert actual == expected
        for threshold in THRESHOLDS:
            assert service.predict(actual, threshold) == direct.predict(expected, threshold)
            trace = service.trace(source, threshold, actual)
            assert len(trace['gates']) == actual['st_n_gates']
            assert [g['index'] for g in trace['gates']] == list(range(len(trace['gates'])))
            assert trace['features'][f'st_log10_cost_{threshold}'] == actual[f'st_log10_cost_{threshold}']


def test_custom_gate_primitive_order_and_three_qubit_limitation(service):
    source = HEADER.replace('q[2]', 'q[3]') + 'gate pair(a) x,y { ry(a) x; cx x,y; }\ngate nested x,y { pair(pi/3) x,y; rz(pi/2) y; }\nnested q[0],q[1];\nccx q[0],q[1],q[2];\n'
    values = service.features(source)
    trace = service.trace(source, 64, values)
    assert [g['name'] for g in trace['gates']] == ['ry', 'cx', 'rz', 'ccx']
    assert [g['qubits'] for g in trace['gates']] == [[0], [0, 1], [1], [0, 1]]
    assert 'first two operands' in trace['note']
    assert values['n_3q_plus'] == values['st_n_3q_plus'] == 1


def test_threshold_changes_native_cost_not_bond_semantics(service):
    source = HEADER.replace('q[2]', 'q[16]') + 'h q;\n' + '\n'.join(f'cx q[{a}],q[{b}];' for _ in range(4) for a in range(8) for b in range(8, 16))
    values = service.features(source)
    low, high = [service.trace(source, threshold, values) for threshold in (16, 1024)]
    assert low['gates'][-1]['total_cost'] < high['gates'][-1]['total_cost']
    assert [(g['bond_before'], g['bond_after']) for g in low['gates']] == [(g['bond_before'], g['bond_after']) for g in high['gates']]
    for result in (low, high):
        assert math.log10(1 + result['gates'][-1]['total_cost']) == pytest.approx(result['features'][f"st_log10_cost_{result['threshold']}"])


def test_trace_limits_fallback_timeout_and_parser_mismatch(service, monkeypatch):
    source = HEADER + 'h q[0];\n' * 10_000
    values = service.features(source)
    assert service.trace(source, 256, values)['expanded_gates'] == 10_000
    values = service.features(source + 'h q[1];\n')
    with pytest.raises(ValueError, match='10,000'):
        service.trace(source, 256, values)
    with pytest.raises(ValueError, match='complete native'):
        service.trace(source, 256, {'st_ok': .75})
    with pytest.raises(ValueError, match='thresholds'):
        service.predict({}, 32)
    with pytest.raises(ValueError, match='thresholds'):
        service.trace(source, 32, {})
    small = service.features(EXAMPLES['bell']['qasm'])
    altered = {**small, 'st_n_gates': small['st_n_gates'] + 1}
    with pytest.raises(ValueError, match='count disagrees'):
        service.trace(EXAMPLES['bell']['qasm'], 256, altered)
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 10
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr('visualizer.dev_model.subprocess.run', timeout)
    with pytest.raises(TimeoutError, match='worker was terminated'):
        service.trace(EXAMPLES['bell']['qasm'], 256, small)


def test_blend_shap_explains_raw_mean_before_postprocessing(service):
    assert service.info['explanation_available'], service.info
    values = service.features(EXAMPLES['partial']['qasm'])
    for threshold in THRESHOLDS:
        explanation = service.explain(values, threshold)
        row = {**values, **service.module.derive(values, threshold)}
        with threadpool_limits(limits=1):
            logs = [float(m.predict(np.array([[float(row.get(c, 0)) for c in cols]]))[0]) for cols, m in service.adapter.members]
        assert explanation['units'] == 'log10(seconds)'
        assert explanation['output'] == pytest.approx(sum(logs) / 4)
        assert explanation['base_value'] + explanation['contribution_sum'] == pytest.approx(explanation['output'], abs=1e-7)
        assert explanation['predicted_seconds'] == service.predict(values, threshold)
        assert len(explanation['contributions']) == 10


def test_timeout_postprocessing_does_not_change_raw_explanation(service):
    # Deliberately chosen synthetic member outputs exercise the exact upstream
    # ANY-member timeout rule while the mean itself is far below the cap.
    class ConstantMember:
        def __init__(self, value):
            self.value = value
        def predict(self, matrix):
            return np.array([self.value])
    class ConstantExplainer:
        def __init__(self, value):
            self.expected_value = value
        def shap_values(self, matrix):
            return np.zeros_like(matrix)
    adapter = service.module.RuntimeModel.__new__(service.module.RuntimeModel)
    adapter.members = [(['log2_threshold'], ConstantMember(value)) for value in (5., 0., 0., 0.)]
    adapter.twin_index, adapter._warned = None, set()
    synthetic = DevModelService.__new__(DevModelService)
    synthetic.adapter, synthetic.module, synthetic.lock = adapter, service.module, service.lock
    synthetic.info = {'explanation_available': True}
    synthetic.explainers = [ConstantExplainer(value) for value in (5., 0., 0., 0.)]
    explanation = synthetic.explain({}, 256)
    assert explanation['output'] == explanation['base_value'] == 1.25
    assert explanation['predicted_seconds'] == 21600
    assert explanation['postprocessing_applied'] is True


def test_session_uses_snapshot_after_runtime_replacement(service):
    # Change only the temporary fixture's build file, never the installed binary.
    installed = Path(service.info['snapshot']).parent.parent / 'runtime/qasm_step.exe'
    original = installed.read_bytes()
    expected = service.features(EXAMPLES['bell']['qasm'])
    try:
        installed.write_bytes(b'replacement from a later build')
        assert service.features(EXAMPLES['bell']['qasm']) == expected
        assert service.trace(EXAMPLES['bell']['qasm'], 256, expected)['expanded_gates'] == 3
    finally:
        installed.write_bytes(original)


def test_changed_source_and_binary_fail_closed(service, tmp_path):
    predictor = tmp_path / 'predictor'
    (predictor / 'artifacts').mkdir(parents=True)
    (predictor / 'artifacts/model.pkl').write_bytes(b'not the approved checkpoint')
    rejected = DevModelService(predictor, tmp_path, ROOT / 'runtime-checkpoint.json')
    assert not rejected.info['available']
    assert 'changed since approval' in rejected.info['error']
    original = service.binary.read_bytes()
    try:
        service.binary.write_bytes(b'changed')
        with pytest.raises(ValueError, match='Session stepper binary changed'):
            service.features(EXAMPLES['bell']['qasm'])
    finally:
        service.binary.write_bytes(original)


def test_manual_stepper_api_contract_cache_authorization_and_capability(service, tmp_path, monkeypatch):
    with TestClient(create_app(Settings(root=tmp_path), model=service)) as client:
        app = client.app
        opened = client.post('/api/examples/custom').json()
        cid = opened['id']
        assert not app.state.results.data and not app.state.traces.data
        assert client.get(f'/api/circuits/{cid}/steps').status_code == 200
        assert not app.state.results.data and not app.state.traces.data
        first = client.post(f'/api/circuits/{cid}/stepper', json={'step': 1, 'threshold': 256})
        assert first.status_code == 200, first.text
        payload = first.json()
        required = {'step', 'threshold', 'cached', 'calculation_seconds', 'expanded_gates', 'features', 'gates', 'note'}
        assert required <= payload.keys()
        assert set(payload['gates'][0]) == {'index','name','qubits','bond_before','bond_after','added_cost','total_cost'}
        assert payload['gates'][0]['index'] == 0
        assert not payload['cached']
        assert 'instruction count' in payload['note']
        def forbidden(*args, **kwargs):
            raise AssertionError('Cached trace reran computation')
        with monkeypatch.context() as patch:
            patch.setattr(service, 'trace', forbidden)
            patch.setattr(service, 'features', forbidden)
            assert client.post(f'/api/circuits/{cid}/stepper', json={'step':1,'threshold':256}).json()['cached']
            assert client.post(f'/api/circuits/{cid}/stepper', json={'step':1,'threshold':32}).status_code == 422
            assert client.post(f'/api/circuits/{cid}/predict', json={'step':1,'threshold':32}).status_code == 422
            assert client.post(f'/api/circuits/{cid}/explain', json={'step':1,'threshold':32}).status_code == 422
        other = client.post(f'/api/circuits/{cid}/stepper', json={'step':1,'threshold':64})
        assert other.status_code == 200 and not other.json()['cached']
        empty = client.post(f'/api/circuits/{cid}/stepper', json={'step':0,'threshold':256})
        assert empty.status_code == 200 and empty.json()['gates'] == []
        assert client.post(f'/api/circuits/{cid}/stepper', json={'step':999,'threshold':256}).status_code == 422
        with monkeypatch.context() as patch:
            patch.setitem(service.info, 'explanation_available', False)
            unavailable = client.post(f'/api/circuits/{cid}/explain', json={'step':1,'threshold':256})
            assert unavailable.status_code == 422 and 'Explanation unavailable' in unavailable.text
        app.state.policy.entries.pop(cid)
        for action in ('features','predict','stepper','explain'):
            assert client.post(f'/api/circuits/{cid}/{action}',json={'step':1,'threshold':256}).status_code == 403


@pytest.mark.parametrize('source', [HEADER + 'if(c==1) x q[0];', 'OPENQASM 3.0;\nqubit[2] q;\nfor int i in [0:1] { x q[i]; }', HEADER + 'include "outside.inc";'])
def test_unsupported_syntax_is_rejected(source):
    with pytest.raises(ValueError):
        parse(source)


@pytest.mark.parametrize('output,match', [
    (b'', 'no output'),
    (b' ' * (4 * 1024 * 1024 + 1), '4 MiB'),
    (b'{"st_ok":0}', 'complete'),
    (b'[]', 'complete'),
    (b'{"st_ok":1,"st_n_gates":1.5}', 'count'),
    (b'{"st_ok":1,"st_n_gates":0,"other":"bad"}', 'numeric'),
    (b'1 h 0 -1 0 0 1 1\n{"st_ok":1,"st_n_gates":1}', 'order'),
    (b'0 h 0 -1 0 0 1 2\n{"st_ok":1,"st_n_gates":1}', 'increments'),
    (b'0 h 0 -1 0 0 nan 1\n{"st_ok":1,"st_n_gates":1}', 'cost'),
    (b'{"st_ok":1,"st_n_gates":1}', 'count'),
])
def test_malformed_native_traces_rejected(output, match):
    with pytest.raises(ValueError, match=match):
        parse_trace(output)
