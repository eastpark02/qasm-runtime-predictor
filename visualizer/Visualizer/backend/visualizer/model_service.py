"""Read-only model adapter. No training, evaluation, corpus traversal or labels."""
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import uuid


class ModelService:
    def __init__(self, pipeline: Path, local: Path):
        self.adapter = None
        self.lock = threading.Lock()
        self.info = {"available": False, "error": None}
        try:
            original = pipeline / 'artifacts'
            before = (original / 'metadata.json').read_bytes()
            metadata = json.loads(before)
            if metadata.get('training_scope') != 'development_only' or metadata.get('audit_opened') is not False:
                raise ValueError("Only an explicitly development-only, audit-unopened checkpoint is permitted")
            artifact = (original / 'model.joblib').read_bytes()
            if before != (original / 'metadata.json').read_bytes():
                raise ValueError("Checkpoint changed during startup; restart after the write completes")
            snapshot = local / 'sessions' / uuid.uuid4().hex
            snapshot.mkdir(parents=True)
            (snapshot / 'model.joblib').write_bytes(artifact)
            (snapshot / 'metadata.json').write_bytes(before)
            sys.path.insert(0, str(pipeline))
            from runtime_pipeline.model import RuntimeModel
            self.adapter = RuntimeModel(artifacts_dir=snapshot)
            self.info = {"available": True, "fingerprint": hashlib.sha256(artifact).hexdigest(),
                         "version": metadata.get('cycle_id', 'development checkpoint'), "training_scope": 'development_only',
                         "rows_fitted": metadata.get('fitted', {}).get('rows_fitted'), "config": metadata.get('model_config', {}),
                         "source": str(original / 'model.joblib'), "loaded_at": time.time(), "error": None}
        except Exception as exc:
            self.info['error'] = f"{type(exc).__name__}: {exc}"

    def require(self):
        if self.adapter is None:
            raise ValueError(f"Runtime model unavailable: {self.info['error']}")

    def features(self, source):
        self.require()
        return self.adapter.featurize(source)

    def predict(self, features, threshold):
        self.require()
        with self.lock:
            return self.adapter.predict(features, threshold)

    def explain(self, features, threshold):
        self.require()
        import numpy as np
        import shap
        from runtime_pipeline.learning import _rows
        with self.lock:
            pipeline = self.adapter.model.model
            if pipeline is None or 'regressor' not in pipeline.named_steps:
                raise ValueError("Local explanations are unavailable for this checkpoint type")
            vectorizer = pipeline.named_steps['vectorizer']
            imputer = pipeline.named_steps['imputer']
            matrix = imputer.transform(vectorizer.transform(_rows([features], [threshold])))
            explainer = shap.TreeExplainer(pipeline.named_steps['regressor'])
            values = np.asarray(explainer.shap_values(matrix))[0]
            names = vectorizer.get_feature_names_out()
            contributions = [{"feature": str(name), "value": float(matrix[0, i]), "contribution": float(values[i])} for i, name in enumerate(names)]
            contributions.sort(key=lambda item: abs(item['contribution']), reverse=True)
            return {"units": "log10(seconds)", "base_value": float(np.asarray(explainer.expected_value).ravel()[0]),
                    "output": float(pipeline.named_steps['regressor'].predict(matrix)[0]),
                    "contributions": contributions[:20],
                    "note": "Tree SHAP explains the un-clipped model output in log10(seconds). These contributions are not additive seconds or causal gate costs. Correlated features can share attribution."}
