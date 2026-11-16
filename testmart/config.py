import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: str
    password: str
    secret: str
    host: str
    port: int
    seed: int


def load() -> Config:
    """Read configuration from the environment on every call (placeholders are dev-only defaults)."""
    default_db = Path(tempfile.gettempdir()) / "testmart" / "testmart.db"
    return Config(
        db_path=os.environ.get("TESTMART_DB", str(default_db)),
        password=os.environ.get("TESTMART_PASSWORD", "testmart"),
        secret=os.environ.get("TESTMART_SECRET", "testmart-dev-secret-placeholder"),
        host=os.environ.get("TESTMART_HOST", "0.0.0.0"),
        port=int(os.environ.get("TESTMART_PORT", "8100")),
        seed=int(os.environ.get("TESTMART_SEED", "42")),
    )
