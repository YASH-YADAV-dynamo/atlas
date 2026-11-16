"""One-time local setup: writes .env (from .env.example) and the HydraDB token file.

Every secret is generated randomly on this machine. Existing values in .env are kept, so it is
safe to run again. Usage: python scripts/setup.py
"""

from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERATED = (
    "HYDRADB_TOKEN",
    "ATLAS_API_KEY",
    "TESTMART_PASSWORD",
    "TESTMART_SECRET",
    "ATLAS_KEY_PEPPER",
    "ATLAS_DEMO_MCP_KEY",
)


def main() -> None:
    env_path = ROOT / ".env"
    source = env_path if env_path.exists() else ROOT / ".env.example"
    lines = source.read_text(encoding="utf8").splitlines()
    out: list[str] = []
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        key = key.strip()
        if (
            sep
            and not line.lstrip().startswith("#")
            and key in GENERATED
            and (not value or value.startswith("replace-"))
        ):
            value = secrets.token_urlsafe(32)
            line = f"{key}={value}"
        if sep and not line.lstrip().startswith("#"):
            values[key] = line.partition("=")[2]
        out.append(line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf8")
    token_path = ROOT / ".hydradb-token"
    token_path.write_text(values["HYDRADB_TOKEN"] + "\n", encoding="utf8")
    token_path.chmod(0o644)  # read by the HydraDB container user
    print("Wrote .env and .hydradb-token with fresh random secrets.")
    if values.get("MODEL_API_KEY", "").startswith("replace-"):
        print(
            "Optional: add a model key to .env (MODEL_API_KEY, MODEL_PROVIDER, ATLAS_MODEL_ID)"
            " so agents can explore pages the map does not know yet."
        )
    print("Next: docker compose up -d --build   then open http://localhost:8000")


if __name__ == "__main__":
    main()
