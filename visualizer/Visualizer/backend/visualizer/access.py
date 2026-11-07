"""Content-based development allowlist. Never opens a circuit corpus or audit file."""
import hashlib
import json
from pathlib import Path

from .examples import EXAMPLES


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AccessPolicy:
    def __init__(self, manifest: Path):
        self.entries = {digest(e['qasm'].encode()): {"origin": "synthetic", "name": key} for key, e in EXAMPLES.items()}
        self.source = "bundled synthetic examples only"
        if manifest.is_file():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("scope") != "development_only":
                raise ValueError("Allowlist must declare development_only scope")
            for key, value in payload.get("circuits", {}).items():
                if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                    raise ValueError("Invalid allowlist content digest")
                if value.get("origin") not in {"development", "synthetic"}:
                    raise ValueError("Unapproved allowlist entry")
                self.entries[key] = value
            self.source = payload.get("source", "development manifest")

    def authorize(self, data: bytes) -> tuple[str, dict]:
        key = digest(data)
        if key not in self.entries:
            raise PermissionError("Upload rejected: this exact file is not on the development/synthetic allowlist. Renaming a file does not approve it. Audit circuits are excluded.")
        return key, self.entries[key]
