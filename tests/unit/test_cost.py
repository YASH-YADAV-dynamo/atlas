"""Edge cost (SPEC 3). Targets atlas.core.cost."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.core.cost import edge_cost, reliability

COUNTS = st.integers(min_value=0, max_value=10_000)
MILLIS = st.floats(min_value=0, max_value=600_000, allow_nan=False)


@given(COUNTS, COUNTS, MILLIS)
def test_cost_follows_the_spec_formula(ok, fail, ms):
    """R-LEARN-1: cost = -ln((ok+1)/(ok+fail+2)) + ms/10000."""
    expected = -math.log((ok + 1) / (ok + fail + 2)) + ms / 10000
    assert edge_cost(ok, fail, ms) == pytest.approx(expected)
    assert reliability(ok, fail) == pytest.approx((ok + 1) / (ok + fail + 2))


@given(COUNTS, COUNTS, MILLIS)
def test_failures_and_slowness_raise_cost_successes_lower_it(ok, fail, ms):
    """R-LEARN-1: reliable, fast edges are cheap."""
    base = edge_cost(ok, fail, ms)
    assert base > 0
    assert edge_cost(ok, fail + 1, ms) > base
    assert edge_cost(ok + 1, fail, ms) < base
    assert edge_cost(ok, fail, ms + 100) > base
