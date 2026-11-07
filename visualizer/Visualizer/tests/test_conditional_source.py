"""Synthetic control-flow upload checks. No corpus or audit data is read."""
import hashlib
import json

from fastapi.testclient import TestClient
import pytest

from visualizer.app import Settings, create_app
from visualizer.circuits import load_qiskit, parse, prefix
from visualizer.workers import _state

HEADER = 'OPENQASM 3.0;\ninclude "stdgates.inc";\nbit[2] meas;\nqubit[2] q;\n'
SOURCE = HEADER + 'h q[1];\nmeas[0] = measure q[1];\nif (meas[0]) { p(pi/2) q[0]; }\nif (meas[0]) {}\n'


def test_conditional_source_steps_prefixes_and_state_boundary():
    parsed = parse(SOURCE)
    assert parsed['inspection_mode'] == 'conditional_source'
    assert (parsed['num_qubits'],parsed['num_clbits'],parsed['state_boundary']) == (2,2,2)
    assert [s['name'] for s in parsed['steps']] == ['h','measure','if_else','if_else']
    assert parsed['steps'][2]['qubits'] == [0]
    assert parsed['steps'][2]['clbits'] == [0]
    assert parsed['steps'][3]['qubits'] == []
    assert prefix(SOURCE,parsed,parsed['total_steps']) == SOURCE
    for step in range(5):
        assert len(load_qiskit(prefix(SOURCE,parsed,step)).data) == step
    state = _state({'qasm':prefix(SOURCE,parsed,1),'pair':[0,1]})
    assert state['qubits'][1]['length'] == pytest.approx(1)
    with pytest.raises(ValueError,match='stops before'):
        _state({'qasm':SOURCE})


def test_conditional_inspection_does_not_invoke_slow_qiskit_import(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Conditional upload invoked Qiskit import')
    monkeypatch.setattr('visualizer.circuits.load_qiskit',forbidden)
    source = HEADER + 'if (meas[0]) { p(pi/2) q[0]; }\n' * 10_000
    result = parse(source)
    assert result['total_steps'] == 10_000
    assert result['state_boundary'] == 1
    assert all(s['line']==5+i for i,s in enumerate(result['steps']))


@pytest.mark.parametrize('statement', [
    'if (unknown) { x q[0]; }',
    'if (meas[2]) { x q[0]; }',
    'if (meas[0]) { x q[2]; }',
    'if (meas[0]) { include "outside.inc"; }',
    'if (meas[0]) { for int i in [0:1] { x q[0]; } }',
    'if (meas[0]) { qubit extra; }',
    'if (meas[0]) { ctrl @ x q[0],q[1]; }',
    'if (meas[0]) { x missing; }',
])
def test_source_mode_rejects_unsupported_or_invalid_constructs(statement):
    with pytest.raises(ValueError):
        parse(HEADER + statement)


def test_nested_conditions_stay_collapsed_and_preserve_source():
    source=HEADER+'if (meas[0]) { if (meas[1]) { x q[1]; } }\n'
    result=parse(source)
    assert result['total_steps']==1
    assert result['steps'][0]['source']==source.splitlines()[-1]
    assert result['steps'][0]['qubits']==[1]
    assert result['steps'][0]['clbits']==[0,1]


def test_conditional_upload_api_is_manual_and_authorized_before_parsing(tmp_path,monkeypatch):
    digest=hashlib.sha256(SOURCE.encode()).hexdigest()
    (tmp_path/'approved-development.json').write_text(json.dumps({'scope':'development_only','circuits':{digest:{'origin':'synthetic'}}}))
    class Model:
        info={'available':True,'fingerprint':'synthetic'}
        calls=0
        def features(self,source):
            self.calls+=1
            return {'n_gates':2,'st_n_gates':2,'st_n_if':2}
        def predict(self,features,threshold):
            self.calls+=1
            return 1.
    model=Model()
    with TestClient(create_app(Settings(root=tmp_path),model=model)) as client:
        response=client.post('/api/circuits?name=conditional.qasm',content=SOURCE)
        assert response.status_code==200,response.text
        opened=response.json()
        assert opened['inspection_mode']=='conditional_source'
        assert opened['state_boundary']==2
        assert any('unevaluated' in note for note in opened['warnings'])
        cid=opened['id']
        assert client.get(f'/api/circuits/{cid}/steps').json()['steps'][2]['name']=='if_else'
        assert model.calls==0
        prediction=client.post(f'/api/circuits/{cid}/predict',json={'step':4,'threshold':256})
        assert prediction.status_code==200,prediction.text
        assert any('unevaluated' in d for d in prediction.json()['diagnostics'])
        assert client.post(f'/api/circuits/{cid}/state',json={'step':2}).status_code==422
        import visualizer.app as app_module
        def forbidden(*args,**kwargs):
            raise AssertionError('Unknown input reached a parser')
        monkeypatch.setattr(app_module,'run_isolated',forbidden)
        assert client.post('/api/circuits',content=SOURCE+'// modified').status_code==403
