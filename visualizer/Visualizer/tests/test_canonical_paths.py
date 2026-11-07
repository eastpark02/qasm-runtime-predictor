"""Canonical app layout is discoverable and environment overrides remain explicit."""
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from visualizer.app import ROOT, Settings, create_app


class StubModel:
    info = {'available': False}


def test_health_reports_actual_application_root(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path), model=StubModel())) as client:
        health = client.get('/api/health')
        assert health.status_code == 200
        assert health.json()['application_root'] == str(tmp_path.resolve())
        assert 'model' in health.json() and 'limits' in health.json()
        assert health.json()['limits']['window_size'] > 0
        assert health.json()['limits']['windows_cached'] > 0


def test_canonical_predictor_default_and_explicit_override(tmp_path):
    env = os.environ.copy()
    env.pop('VISUALIZER_PREDICTOR', None)
    env['PYTHONPATH'] = str(ROOT / 'backend')
    command = [sys.executable, '-c', 'from visualizer.app import Settings; print(Settings().predictor)']
    default = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30)
    assert Path(default.stdout.strip()) == ROOT.parents[1]
    env['VISUALIZER_PREDICTOR'] = str(tmp_path / 'explicit-predictor')
    override = subprocess.run(command, env=env, capture_output=True, text=True, check=True, timeout=30)
    assert Path(override.stdout.strip()) == tmp_path / 'explicit-predictor'
