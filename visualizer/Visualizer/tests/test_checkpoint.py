"""Synthetic-only integration with the requested checkpoint. Never reads labels."""
from pathlib import Path
import pytest

from visualizer.circuits import parse, prefix
from visualizer.examples import EXAMPLES
from visualizer.model_service import ModelService


def test_requested_checkpoint_adapter_agreement_and_fixed_session(tmp_path):
    pipeline = Path(__file__).resolve().parents[2] / 'ml-agent-pipeline'
    if not (pipeline / 'artifacts' / 'model.joblib').is_file():
        pytest.skip('Requested development checkpoint not installed next to this checkout')
    service = ModelService(pipeline, tmp_path)
    assert service.info['available'], service.info
    source = EXAMPLES['partial']['qasm']
    parsed = parse(source)
    from runtime_pipeline.model import RuntimeModel
    direct = RuntimeModel(artifacts_dir=pipeline / 'artifacts')
    features = service.features(prefix(source, parsed, parsed['total_steps']))
    assert features == direct.featurize(source)
    assert service.predict(features, 256) == pytest.approx(direct.predict(features, 256))
    assert service.info['training_scope'] == 'development_only'
    assert len(service.info['fingerprint']) == 64
    assert list((tmp_path / 'sessions').glob('*/model.joblib'))
    explanation = service.explain(features, 256)
    assert explanation['units'] == 'log10(seconds)'
    assert explanation['output'] == pytest.approx(__import__('math').log10(service.predict(features, 256)))
