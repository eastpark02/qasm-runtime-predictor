"""App-owned resource limits, independent of predictor feature extraction."""
DEFAULT_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_CONFIGURED_UPLOAD_BYTES = 256 * 1024 * 1024
INTERACTIVE_BYTES = 32 * 1024 * 1024
PYTHON_FALLBACK_BYTES = 5 * 1024 * 1024
METADATA_PREVIEW_CHARS = 4096
STATE_MEMORY_BYTES = 1024 ** 3
STATE_VECTOR_QUBITS = 22
STATE_STABILIZER_QUBITS = 512
STATE_MPS_QUBITS = 128
STATE_OPERATIONS = 10_000
STATE_JOINT_QUBITS = 12
STATE_TIMEOUT_SECONDS = 30


def validate_upload_bytes(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CONFIGURED_UPLOAD_BYTES:
        raise ValueError('VISUALIZER_MAX_UPLOAD_BYTES must be an integer from 1 through 268435456 (256 MiB)')
    return value


def preview(text):
    return text[:METADATA_PREVIEW_CHARS] if text is not None else None
