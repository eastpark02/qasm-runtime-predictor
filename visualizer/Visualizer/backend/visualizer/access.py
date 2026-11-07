"""Content-based development allowlist. Never opens a circuit corpus or audit file."""
import hashlib
import json
from pathlib import Path

from .examples import EXAMPLES


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


UNLISTED_WARNING = 'Unlisted upload: this file is not on the development allowlist, so audit exclusion is not guaranteed for it.'
FLAG_ON, FLAG_OFF = {'1', 'true', 'yes', 'on'}, {'', '0', 'false', 'no', 'off'}


def parse_flag(value, name='VISUALIZER_ALLOW_UNLISTED_UPLOADS') -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text not in FLAG_ON | FLAG_OFF:
        raise ValueError(f'{name} must be one of 1/true/yes/on or 0/false/no/off (default off)')
    return text in FLAG_ON


class AccessPolicy:
    def __init__(self, manifest: Path, allow_unlisted: bool = False):
        # Runtime switch, off by default. It never adds entries: the manifest
        # (and this in-memory copy of it) is never modified.
        self.allow_unlisted = bool(allow_unlisted)
        self.entries = {digest(e['qasm'].encode()): {"origin": "synthetic", "name": key} for key, e in EXAMPLES.items()}
        self.source = "bundled synthetic examples only"
        if manifest.is_file():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("scope") != "development_only":
                raise ValueError("Allowlist must declare development_only scope")
            for key, value in payload.get("circuits", {}).items():
                if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                    raise ValueError("Invalid allowlist content digest")
                if value.get("origin") not in {"development", "synthetic"}:  # A manifest can never grant 'unlisted'.
                    raise ValueError("Unapproved allowlist entry")
                self.entries[key] = value
            self.source = payload.get("source", "development manifest")

    def authorize(self, data: bytes) -> tuple[str, dict]:
        return self.authorize_digest(digest(data))

    @property
    def scope(self) -> str:
        return 'unrestricted' if self.allow_unlisted else 'development_only'

    def authorize_digest(self, key: str) -> tuple[str, dict]:
        if key not in self.entries:
            if self.allow_unlisted:
                return key, {'origin': 'unlisted', 'name': None}
            raise PermissionError("Upload rejected: this exact file is not on the development/synthetic allowlist. Renaming a file does not approve it. Audit circuits are excluded.")
        return key, self.entries[key]
