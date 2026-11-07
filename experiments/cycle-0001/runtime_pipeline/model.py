"""Challenge harness adapter."""

from pathlib import Path
import joblib

from .parser import featurize


class RuntimeModel:
    def __init__(self, artifacts_dir=None):
        directory = Path(artifacts_dir) if artifacts_dir is not None else Path(__file__).resolve().parents[1] / "artifacts"
        artifact = directory / "model.joblib"
        if not artifact.is_file():
            raise FileNotFoundError(f"Trained artifact missing: {artifact}. Train or finalize the pipeline first.")
        self.model = joblib.load(artifact)

    def featurize(self, qasm_text: str) -> dict:
        return featurize(qasm_text)

    def predict(self, features: dict, threshold: int) -> float:
        return float(self.model.predict([features], [threshold])[0])
