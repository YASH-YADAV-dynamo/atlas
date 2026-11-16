"""Parsing of the real algo.SPpaths response saved by the Stage 1 probe.

Targets atlas.adapters.hydradb.paths.parse_path_rows(rows) -> tuple[RawPath, ...], where a
RawPath has `node_ids: tuple[int, ...]` (in path order) and `cost: float`.

ASSUMPTION TO RE-VERIFY when the probe lane lands the fixture (architect re-checks, see
docs/architecture.md "Open assumptions"): tests/fixtures/sppaths_response.json is

    {"source": <int>, "target": <int>,
     "rows": [{"path": {"nodes": [{"id": <int>, ...}, ...],
                        "relationships": [{"src": <int>, "dst": <int>, ...}, ...]},
               "pathCost": <number>}, ...]}

`rows` is what the adapter passes to parse_path_rows after converting each Bolt record to
plain data. If the probe saved a different shape, the architect updates this test, not the lane.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sppaths_response.json"


@pytest.fixture
def saved() -> dict:
    if not FIXTURE.exists():
        pytest.skip("tests/fixtures/sppaths_response.json not merged yet (probe lane P1)")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_real_sppaths_response_parses(saved):
    """R-PATH-1: every saved path parses to ordered node ids and a numeric cost."""
    from atlas.adapters.hydradb.paths import parse_path_rows

    paths = parse_path_rows(saved["rows"])
    assert len(paths) == len(saved["rows"]) >= 1
    for parsed, row in zip(paths, saved["rows"], strict=True):
        assert list(parsed.node_ids) == [node["id"] for node in row["path"]["nodes"]]
        assert len(parsed.node_ids) == len(row["path"]["relationships"]) + 1
        assert len(set(parsed.node_ids)) == len(parsed.node_ids)
        assert parsed.cost == pytest.approx(float(row["pathCost"]))
        assert (parsed.node_ids[0], parsed.node_ids[-1]) == (saved["source"], saved["target"])


def test_malformed_path_is_a_typed_error():
    """R-PATH-2: a row the adapter cannot interpret raises PathFormatError, never a KeyError."""
    from atlas.adapters.hydradb.paths import parse_path_rows
    from atlas.contracts.errors import PathFormatError

    for bad in (
        [{"pathCost": 1.0}],
        [{"path": {"nodes": "nope"}, "pathCost": 1.0}],
        [{"path": {"nodes": []}, "pathCost": 1.0}],
    ):
        with pytest.raises(PathFormatError):
            parse_path_rows(bad)
