"""The SDK version has one source (sdk/pyproject.toml, read at runtime via importlib.metadata)
and the Dockerfile builds and serves its wheel."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_builds_sdk_wheel() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY sdk ./sdk" in text
    assert text.index("COPY sdk ./sdk") < text.index("pip wheel ./sdk")
    assert "-w web/sdk" in text
