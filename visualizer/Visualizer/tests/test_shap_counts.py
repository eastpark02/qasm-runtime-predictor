"""SHAP output cardinality uses model columns, independent of derived inputs.

Synthetic members exercise aggregation/ranking without loading an artifact or
reading circuit data. End-to-end pinned-artifact checks live in test_dev_model.
"""
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from visualizer.dev_model import DevModelService


def service_for(contributions):
    columns = [f'raw_{i}' for i in range(len(contributions))]
    output = sum(contributions)
    member = SimpleNamespace(predict=lambda matrix: np.array([output]))
    explainer = SimpleNamespace(expected_value=0., shap_values=lambda matrix: np.array([contributions]))
    service = DevModelService.__new__(DevModelService)
    service.adapter = SimpleNamespace(members=[(columns, member)] * 4,
                                      predict=lambda features, threshold: 10 ** output)
    service.module = SimpleNamespace(derive=lambda features, threshold: {f'derived_{i}': i for i in range(7)},
                                     TIMEOUT_RULE_FRAC=.6, CAP_SECONDS=21600)
    service.explainers = [explainer] * 4
    service.lock = threading.Lock()
    service.info = {'explanation_available': True, 'fingerprint': 'synthetic'}
    return service


@pytest.mark.parametrize('count', [0, 1, 7, 10, 13])
def test_explanation_returns_at_most_ten_real_columns(count):
    contributions = [(-1 if i % 2 else 1) * (i + 1) / 100 for i in range(count)]
    service = service_for(contributions)
    result = service.explain({f'raw_{i}': i for i in range(count)}, 256)
    assert result['contribution_count'] == count
    assert len(result['contributions']) == min(10, count)
    assert [item['feature'] for item in result['contributions']] == [f'raw_{i}' for i in reversed(range(count))][:10]
    assert result['contribution_sum'] == pytest.approx(sum(contributions))
    assert result['base_value'] + result['contribution_sum'] == pytest.approx(result['output'])
    assert all(item['value'] == int(item['feature'].split('_')[1]) for item in result['contributions'])


def test_zero_attribution_remains_a_real_model_input():
    result = service_for([0.]).explain({'raw_0': 0.}, 256)
    assert result['contribution_count'] == 1
    assert result['contributions'] == [{'feature': 'raw_0', 'value': 0., 'contribution': 0.}]
