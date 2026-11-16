"""Locate -> plan -> report against a real HydraDB. Skipped cleanly when HYDRADB_URL is unset.

Targets atlas.adapters.hydradb.store.open_store(url, token, graph) and the Route API.
"""

from __future__ import annotations

import pytest

from tests.support.builders import GOAL, click, learn, page, state_id_of
from tests.support.scenarios import client_for, locate_plan_report_round_trip

pytestmark = pytest.mark.integration


def test_round_trip_on_real_hydradb(hydra_store, ns):
    """R-INT-1: the Route API round trip passes with HydraDB doing the planning."""
    locate_plan_report_round_trip(client_for(hydra_store, ns))


def test_cost_update_changes_the_route_on_real_hydradb(hydra_store, ns):
    """R-INT-2 (R-PLAN-2 on the engine): SET cost changes what algo.SPpaths returns next."""
    from atlas.contracts.api_models import PlanRequest
    from atlas.core.planner import plan

    a = page("/a", "button:to-b", "button:shortcut")
    b, c, d = page("/b", "button:to-c"), page("/c", "button:finish"), page("/d", "button:done")
    learn(hydra_store, ns, a, click("shortcut"), c, ms=10_000)
    learn(hydra_store, ns, a, click("to-b"), b)
    learn(hydra_store, ns, b, click("to-c"), c)
    learn(hydra_store, ns, c, click("finish"), d, goal_text=GOAL, goal_reached=True)
    request = PlanRequest(site="shop.test", state_id=state_id_of(hydra_store, ns, a), goal_text=GOAL)

    def route() -> list[str]:
        return [s.action.locator.name for s in plan(hydra_store, ns, request).route if s.action.locator]

    assert route() == ["to-b", "to-c", "finish"]
    b_id = state_id_of(hydra_store, ns, b)
    for _ in range(5):
        learn(hydra_store, ns, a, click("to-b"), a, ok=False, expect_state=b_id)
    assert route() == ["shortcut", "finish"]
