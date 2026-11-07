"""App-owned resource limits, independent of predictor feature extraction."""
DEFAULT_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_CONFIGURED_UPLOAD_BYTES = 256 * 1024 * 1024
INTERACTIVE_BYTES = 32 * 1024 * 1024
PYTHON_FALLBACK_BYTES = 5 * 1024 * 1024
METADATA_PREVIEW_CHARS = 4096


def validate_upload_bytes(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONFIGURED_UPLOAD_BYTES:
        raise ValueError('VISUALIZER_MAX_UPLOAD_BYTES must be an integer from 1 through 268435456 (256 MiB)')
    return value


def preview(text):
    return text[:METADATA_PREVIEW_CHARS] if text is not None else None
