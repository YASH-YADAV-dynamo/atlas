"""Query catalogue safety. Targets atlas.adapters.hydradb.queries."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.adapters.hydradb import queries
from atlas.contracts.errors import UnsafeQueryValueError

CATALOGUE = {
    name: value for name, value in vars(queries).items() if name.isupper() and isinstance(value, str)
}
STATEMENTS = {name: text for name, text in CATALOGUE.items() if " " in text}


@given(st.integers(min_value=-(10**6), max_value=10**6), st.integers(0, 50), st.integers(51, 100))
def test_safe_int_accepts_only_integers_in_range(value, low, high):
    """R-SEC-1: only a plain int inside the bounds may ever be inlined into Cypher."""
    if low <= value <= high:
        assert queries.safe_int(value, low, high) == value
    else:
        with pytest.raises(UnsafeQueryValueError):
            queries.safe_int(value, low, high)


@pytest.mark.parametrize("value", ["3", "3}) MATCH (n) DETACH DELETE n //", 3.0, True, None, [3], b"3"])
def test_safe_int_rejects_everything_that_is_not_an_int(value):
    """R-SEC-1: strings (including injection attempts), floats, bools and lists are refused."""
    with pytest.raises(UnsafeQueryValueError):
        queries.safe_int(value, 0, 100)


def test_planning_query_inlines_only_validated_bounds():
    """R-SEC-1, R-PLAN-4, R-PLAN-8: source/target are parameters; maxLen counts edges
    (2 * steps - 1) and never exceeds the engine limit of 16."""
    text = queries.sppaths(max_steps=8, path_count=5)
    assert "sourceNode: $source" in text and "targetNode: $target" in text
    assert "maxLen: 15" in text and "pathCount: 5" in text
    assert queries.edges_for_steps(1) == 1
    for bad in ("8", 0, -1, 9, 10**6, True):
        with pytest.raises(UnsafeQueryValueError):
            queries.sppaths(max_steps=bad, path_count=5)  # type: ignore[arg-type]
    for bad in ("5", 0, -1, 10**6, True):
        with pytest.raises(UnsafeQueryValueError):
            queries.sppaths(max_steps=8, path_count=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_catalogue_statements_are_static_and_parameterised(name):
    """R-SEC-1, R-STORE-5: one statement each, no format holes, no string literals for data."""
    text = STATEMENTS[name]
    assert ";" not in text
    assert "{}" not in text and "%s" not in text and "{0}" not in text
    assert '"' not in text and "'" not in text
    assert "$" in text


def test_writes_respect_the_engine_rules():
    """R-STORE-5: MERGE carries ids only; properties are applied by a separate MATCH ... SET."""
    for name, text in STATEMENTS.items():
        for pattern in re.findall(r"MERGE (.*?)(?: SET |$)", text):
            properties = set(re.findall(r"(\w+):\s*(?:\$|row\.)", pattern))
            assert properties <= {"id"}, name
        assert "ON CREATE" not in text and "ON MATCH" not in text, name
        assert not re.search(r"\bIN\b|CONTAINS|\*\d*\.\.\s*\]", text), name
