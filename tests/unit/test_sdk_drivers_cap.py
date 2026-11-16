"""R-SDK-3 for the optional drivers: they stay small and standalone, and the old import path works."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("atlas_sdk")

DRIVERS = Path(__file__).resolve().parents[2] / "sdk" / "atlas_sdk" / "drivers"


def test_sdk_drivers_are_small_and_standalone():
    paths = sorted(DRIVERS.rglob("*.py"))
    assert paths
    code_lines = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "from atlas." not in text and "import atlas." not in text and "from atlas import" not in text
        code_lines += sum(
            1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#")
        )
    assert code_lines <= 200


def test_old_playwright_import_path_still_works():
    from atlas_sdk.drivers.playwright import PlaywrightDriver
    from atlas_sdk.playwright import PlaywrightDriver as Reexported

    assert Reexported is PlaywrightDriver
