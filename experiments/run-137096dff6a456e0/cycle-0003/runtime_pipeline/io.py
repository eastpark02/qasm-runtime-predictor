"""Read the challenge's streaming zstandard frames without simulating circuits."""

from pathlib import Path


def read_qasm(path: str | Path) -> str:
    path = Path(path)
    if path.name.endswith(".qasm.zst"):
        import zstandard
        with path.open("rb") as source:
            with zstandard.ZstdDecompressor().stream_reader(source) as stream:
                return stream.read().decode("utf-8", "replace")
    return path.read_text(encoding="utf-8-sig", errors="replace")


def circuit_name(path: str | Path) -> str:
    """Output join key only; never supplied to the estimator as a feature."""
    name = Path(path).name
    return name[:-4] if name.endswith(".qasm.zst") else name
