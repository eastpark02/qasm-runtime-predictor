"""Pinned inference-only integration with the requested upstream dev checkpoint.

Only the six manifest files and our local binary are read. No training module,
label table, corpus listing, upstream CV results, or structural-twin index is used.
"""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .stepper import parse_trace

THRESHOLDS = (16, 64, 256, 512, 1024)
TRACE_LIMIT = 10_000
FILES = ('artifacts/model.pkl', 'submission/model.py', 'qasm_features.py',
         'qasm_step.py', 'qasm_step_py.py', 'stepper/qasm_step.cpp')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class DevModelService:
    def __init__(self, predictor: Path, local: Path, manifest_path: Path):
        self.adapter = None
        self.lock = threading.Lock()
        self.info = {'available': False, 'error': None, 'provider': 'qasm-runtime-predictor',
                     'stepper_available': False, 'explanation_available': False}
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if manifest.get('raw_label_index') is not False or set(manifest['files']) != set(FILES):
                raise ValueError('Expected the explicitly pinned inference-only dev checkpoint manifest')
            payloads = {}
            for name in FILES:
                data = (predictor / name).read_bytes()
                if sha256(data) != manifest['files'][name]:
                    raise ValueError(f'{name} changed since approval. Review and pin the new dev revision before restarting.')
                payloads[name] = data
            build = json.loads((local / 'runtime' / 'build.json').read_text(encoding='utf-8-sig'))
            binary = (local / 'runtime' / 'qasm_step.exe').read_bytes()
            if build.get('source_sha256') != manifest['files']['stepper/qasm_step.cpp'] or build.get('binary_sha256') != sha256(binary):
                raise ValueError('C++ stepper build does not match the pinned source. Run scripts/build-stepper.ps1.')
            snapshot = local / 'sessions' / uuid.uuid4().hex
            (snapshot / 'artifacts').mkdir(parents=True)
            for name, data in payloads.items():
                destination = snapshot / ('artifacts/model.pkl' if name == 'artifacts/model.pkl' else Path(name).name)
                destination.write_bytes(data)
            self.binary = snapshot / 'qasm_step.exe'
            self.binary.write_bytes(binary)
            (snapshot / 'checkpoint.json').write_text(json.dumps(manifest, indent=2))
            # Every import comes from the verified session snapshot. The upstream
            # RuntimeModel itself is reused, including derive() and timeout rules.
            sys.path.insert(0, str(snapshot))
            for name in ('qasm_features', 'qasm_step_py', 'qasm_step'):
                spec = importlib.util.spec_from_file_location(name, snapshot / f'{name}.py')
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            spec = importlib.util.spec_from_file_location(f'visualizer_runtime_{snapshot.name}', snapshot / 'model.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            from threadpoolctl import threadpool_limits
            with threadpool_limits(limits=1):
                adapter = module.RuntimeModel(artifacts_dir=str(snapshot / 'artifacts'))
            if adapter.twin_index is not None:
                raise ValueError('Raw-label structural-twin indices are not supported by this visualizer')
            from sklearn.ensemble import HistGradientBoostingRegressor
            if len(adapter.members) != 4 or any(not isinstance(m, HistGradientBoostingRegressor) for _, m in adapter.members):
                raise ValueError('Expected the pinned four-member HistGradientBoosting blend')
            self.binary_fingerprint = sha256(binary)
            self.adapter, self.module = adapter, module
            # Capability detection is independent of any user circuit. Errors are
            # visible in health and manual explanations still verify additivity.
            try:
                import shap
                self.explainers = [shap.TreeExplainer(m) for _, m in adapter.members]
                self.info['explanation_available'] = True
            except Exception as exc:
                self.explainers = None
                self.info['explanation_error'] = f'{type(exc).__name__}: {exc}'
            self.info.update({'available': True, 'stepper_available': True,
                              'fingerprint': manifest['files']['artifacts/model.pkl'],
                              'version': manifest['version'], 'commit': manifest['commit'],
                              'training_scope': manifest['training_scope'], 'rows_fitted': manifest['rows_fitted'],
                              'circuits_fitted': manifest['circuits_fitted'], 'raw_label_index': False,
                              'source_fingerprints': dict(manifest['files']),
                              'provenance_note': manifest['provenance_note'],
                              'source': str(predictor / 'artifacts/model.pkl'),
                              'config': {'kind': 'HistGradientBoosting blend', 'members': len(adapter.members)},
                              'thresholds': list(THRESHOLDS), 'loaded_at': time.time(),
                              'stepper_fingerprint': sha256(binary), 'snapshot': str(snapshot),
                              'trace_operation_limit': TRACE_LIMIT})
        except Exception as exc:
            self.info['error'] = f'{type(exc).__name__}: {exc}'

    def require(self):
        if self.adapter is None:
            raise ValueError(f"Runtime model unavailable: {self.info['error']}")

    def check_binary(self):
        # Native extraction launches this file on each request. Refuse mutation
        # inside the private snapshot rather than adopting different executable bytes.
        if sha256(self.binary.read_bytes()) != self.binary_fingerprint:
            raise ValueError('Session stepper binary changed; restart with a verified build')

    def features(self, source):
        self.require()
        self.check_binary()
        with self.lock:
            values = self.adapter.featurize(source)
        if any(not math.isfinite(float(value)) for value in values.values()):
            raise ValueError('Model extractor returned a non-finite feature')
        return values

    def features_file(self, path, input_hash, timeout=180, memory_bytes=3 * 1024 * 1024 * 1024):
        """Same pinned adapter, isolated so large inputs cannot exhaust the app."""
        self.require()
        self.check_binary()
        from .workers import run_isolated
        values, _ = run_isolated('full_file_features', {
            'source_path': str(path), 'input_hash': input_hash, 'snapshot': self.info['snapshot'],
            'binary_sha256': self.binary_fingerprint, 'memory_bytes': memory_bytes}, timeout)
        return values

    def predict(self, features, threshold):
        self.require()
        if threshold not in THRESHOLDS:
            raise ValueError(f'This checkpoint supports simulator thresholds {THRESHOLDS}')
        from threadpoolctl import threadpool_limits
        with self.lock, threadpool_limits(limits=1):
            return self.adapter.predict(features, threshold)

    def derived(self, features, threshold):
        self.require()
        if threshold not in THRESHOLDS:
            raise ValueError(f'This checkpoint supports simulator thresholds {THRESHOLDS}')
        return self.module.derive(features, threshold)

    def inputs(self, features, threshold):
        """Exact per-member columns/values, including upstream missing-column zeros."""
        row = {**features, **self.derived(features, threshold)}
        return [{'member': index, 'values': {name: float(row.get(name, 0.0)) for name in columns}}
                for index, (columns, _) in enumerate(self.adapter.members)]

    def explain(self, features, threshold):
        self.require()
        if threshold not in THRESHOLDS:
            raise ValueError(f'This checkpoint supports simulator thresholds {THRESHOLDS}')
        if not self.info.get('explanation_available'):
            raise ValueError(self.info.get('explanation_error', 'Tree SHAP is unavailable for this blend'))
        import numpy as np
        from threadpoolctl import threadpool_limits
        row = {**features, **self.module.derive(features, threshold)}
        contributions, baseline, output = {}, 0.0, 0.0
        count = len(self.adapter.members)
        with self.lock, threadpool_limits(limits=1):
            for (cols, estimator), explainer in zip(self.adapter.members, self.explainers):
                matrix = np.array([[float(row.get(c, 0.0)) for c in cols]])
                values = np.asarray(explainer.shap_values(matrix))[0]
                baseline += float(np.asarray(explainer.expected_value).ravel()[0]) / count
                output += float(estimator.predict(matrix)[0]) / count
                for name, value in zip(cols, values):
                    contributions[name] = contributions.get(name, 0.0) + float(value) / count
            predicted = self.adapter.predict(features, threshold)
        if not math.isclose(baseline + sum(contributions.values()), output, rel_tol=1e-7, abs_tol=1e-7):
            raise ValueError('Tree SHAP additivity failed for the raw blend log10 output')
        items = [{'feature': name, 'value': float(row.get(name, 0.0)), 'contribution': value} for name, value in contributions.items()]
        items.sort(key=lambda item: abs(item['contribution']), reverse=True)
        return {'units': 'log10(seconds)', 'base_value': baseline, 'output': output,
                'contribution_sum': sum(contributions.values()), 'predicted_seconds': predicted,
                'postprocessing_applied': not math.isclose(output, math.log10(predicted), abs_tol=1e-10),
                'raw_output': output, 'raw_predicted_seconds': 10 ** output,
                'model_fingerprint': self.info.get('fingerprint'),
                'postprocessing': {'timeout_rule_fraction': self.module.TIMEOUT_RULE_FRAC,
                                   'cap_seconds': self.module.CAP_SECONDS,
                                   'applied': not math.isclose(output, math.log10(predicted), abs_tol=1e-10)},
                'contribution_count': len(items), 'contributions': items[:10],
                'note': 'Mean Tree SHAP across the four members explains the blend before the timeout rule and six-hour cap. Units are log10(seconds), not additive seconds or causal gate costs. Up to 10 signed contributions by absolute magnitude are shown, or all if fewer are available; contribution_sum includes every feature. Raw features and derived inputs are both eligible. Correlated inputs can share attribution.'}

    def trace(self, source, threshold, features):
        self.require()
        if threshold not in THRESHOLDS:
            raise ValueError(f'This checkpoint supports simulator thresholds {THRESHOLDS}')
        if features.get('st_ok') != 1 or features.get('st_truncated'):
            raise ValueError('A complete native C++ feature pass is required before tracing; fallback/truncated output cannot be traced')
        if features.get('st_n_gates', TRACE_LIMIT + 1) > TRACE_LIMIT:
            raise ValueError(f'C++ trace is limited to {TRACE_LIMIT:,} expanded gates. Select an earlier source checkpoint; feature extraction and prediction remain available.')
        self.check_binary()
        started = time.perf_counter()
        # A temporary directory isolates each invocation, and timeout kills the
        # subprocess. The preflight and parser cap the trace to 10,000 gates.
        with tempfile.TemporaryDirectory(prefix='visualizer-stepper-') as directory:
            path = Path(directory) / 'prefix.qasm'
            path.write_bytes(source.encode('utf-8'))
            try:
                completed = subprocess.run([str(self.binary), '--trace', '--thresholds', str(threshold), str(path)],
                                           stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError('C++ stepper trace exceeded 10 seconds; the worker was terminated') from exc
            if completed.returncode:
                raise ValueError('C++ stepper failed: ' + completed.stderr.decode('utf-8', errors='replace')[:500])
        result = parse_trace(completed.stdout, TRACE_LIMIT)
        if result['expanded_gates'] != features['st_n_gates']:
            raise ValueError('Native trace count disagrees with the pinned adapter feature pass')
        cost_key = f'st_log10_cost_{threshold}'
        if not math.isclose(result['features'][cost_key], features[cost_key], rel_tol=1e-10, abs_tol=1e-10):
            raise ValueError('Native trace cost disagrees with the pinned adapter feature pass')
        result.update({'threshold': threshold, 'calculation_seconds': time.perf_counter() - started,
                       'note': 'Structural MPS workload heuristic in arbitrary cost units. Bond fields describe the first affected cut as log2 bond dimension (zero for a one-qubit gate), not entanglement. This is not a quantum-state simulation. Expanded indices are separate from source steps. Only the first two operands are reported for gates acting on three or more qubits.'})
        if features.get('st_n_if') or features.get('n_if_blocks'):
            result['note'] += ' Conditional gates are structural written-body work; no measurement outcomes or branch choices are simulated. This is not an executed branch trace.'
        return result
