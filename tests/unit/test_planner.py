"""Planner. Targets atlas.core.planner.plan, with maps built through the public
locate/report functions on the in-memory store.

Map used by most tests (costs follow the spec formula, one success each):

    A --to-b (100 ms)--> B --to-c (100 ms)--> C --finish--> D     goal: reached by "finish"
    A --shortcut (10 000 ms)------------------> C

Via B costs about 0.83, the shortcut about 1.41, so the cheapest route goes through B.
"""

from __future__ import annotations

import pytest

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.errors import UnknownStateError
from atlas.contracts.ports import GraphStore
from atlas.contracts.types import (
    MAX_ROUTE_STEPS,
    Action,
    ActionKind,
    Fill,
    Locator,
    PlanReason,
    PlanResult,
)
from atlas.core.params import bind_action
from atlas.core.planner import plan
from tests.support.builders import GOAL, SITE, click, learn, page, state_id_of

A = page("/a", "button:to-b", "button:shortcut")
B = page("/b", "button:to-c")
C = page("/c", "button:finish")
D = page("/d", "button:done")
ISLAND = page("/island", "button:nothing")


def build_map(store: GraphStore, ns: str) -> None:
    learn(store, ns, A, click("shortcut"), C, ms=10_000)
    learn(store, ns, A, click("to-b"), B)
    learn(store, ns, B, click("to-c"), C)
    learn(store, ns, C, click("finish"), D, goal_text=GOAL, goal_reached=True)


def plan_from(
    store: GraphStore, ns: str, snapshot, goal: str = GOAL, params=None, **bounds: int
) -> PlanResult:
    request = PlanRequest(
        site=SITE, state_id=state_id_of(store, ns, snapshot), goal_text=goal, params=params or {}
    )
    return plan(store, ns, request, **bounds)


def names(result: PlanResult) -> list[str]:
    return [step.action.locator.name for step in result.route if step.action.locator]


def test_picks_the_cheapest_route(fake_store, ns):
    """R-PLAN-1: the route is the cheapest path, ending with the action that achieves the goal."""
    build_map(fake_store, ns)
    result = plan_from(fake_store, ns, A)
    assert names(result) == ["to-b", "to-c", "finish"]
    assert [s.expect_state for s in result.route] == [state_id_of(fake_store, ns, p) for p in (B, C, D)]
    assert result.needs_model is False
    assert result.reason is PlanReason.ROUTE_FOUND
    assert 0 < result.confidence < 1
    assert all(0 < step.reliability < 1 for step in result.route)


def test_cost_update_changes_the_route(fake_store, ns):
    """R-PLAN-2: after an edge on the route fails repeatedly, the next plan avoids it."""
    build_map(fake_store, ns)
    b_id = state_id_of(fake_store, ns, B)
    for _ in range(5):
        learn(fake_store, ns, A, click("to-b"), A, ok=False, expect_state=b_id)
    assert names(plan_from(fake_store, ns, A)) == ["shortcut", "finish"]


def test_goal_on_the_current_page_still_needs_its_final_action(fake_store, ns):
    """R-PLAN-1: the goal is an action, not a page, so standing on D does not mean 'done'."""
    build_map(fake_store, ns)
    learn(fake_store, ns, D, click("done"), C)
    assert names(plan_from(fake_store, ns, D)) == ["done", "finish"]


def test_unknown_goal_needs_the_model(fake_store, ns):
    """R-PLAN-3: a goal the map has never seen returns an empty route and needs_model."""
    build_map(fake_store, ns)
    result = plan_from(fake_store, ns, A, goal="something nobody has done")
    assert result.route == ()
    assert result.needs_model is True
    assert result.reason is PlanReason.UNKNOWN_GOAL
    assert result.confidence == 0


def test_no_route_from_here_needs_the_model(fake_store, ns):
    """R-PLAN-3: a known goal that cannot be reached from the current state needs the model."""
    build_map(fake_store, ns)
    learn(fake_store, ns, ISLAND, click("nothing"), ISLAND)
    result = plan_from(fake_store, ns, ISLAND)
    assert result.route == ()
    assert result.needs_model is True
    assert result.reason is PlanReason.NO_ROUTE_WITHIN_MAX_STEPS


def chain(store: GraphStore, ns: str, length: int) -> list:
    pages = [page(f"/step/{name}", f"button:go-{name}") for name in "abcdefghij"[: length + 1]]
    for index in range(length):
        last = index == length - 1
        learn(
            store,
            ns,
            pages[index],
            click(f"go-{'abcdefghij'[index]}"),
            pages[index + 1],
            goal_text=GOAL if last else None,
            goal_reached=last,
        )
    return pages


def test_too_small_a_bound_is_widened_not_reported_as_empty(fake_store, ns):
    """R-PLAN-4: when the first bound finds nothing, the planner widens it up to the cap."""
    pages = chain(fake_store, ns, 7)
    result = plan_from(fake_store, ns, pages[0], max_steps=2, max_steps_cap=8)
    assert len(result.route) == 7
    assert result.needs_model is False
    assert 7 <= result.max_steps_used <= 8


def test_bound_exhaustion_is_reported_with_the_bound_used(fake_store, ns):
    """R-PLAN-4: giving up says so: the reason and the largest bound searched are returned."""
    pages = chain(fake_store, ns, 7)
    result = plan_from(fake_store, ns, pages[0], max_steps=2, max_steps_cap=4)
    assert result.route == ()
    assert result.needs_model is True
    assert result.reason is PlanReason.NO_ROUTE_WITHIN_MAX_STEPS
    assert result.max_steps_used == 4


class BoundSpy:
    """Wraps a store and records every step bound the planner asks it for."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self.bounds: list[int] = []

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def shortest_paths(self, ns, source, target, max_steps, path_count):
        self.bounds.append(max_steps)
        return self._store.shortest_paths(ns, source, target, max_steps, path_count)


@pytest.mark.xfail(reason="pending R-PLAN-8: planner clamp to MAX_ROUTE_STEPS (core lane)", strict=False)
def test_planner_never_asks_for_more_steps_than_the_engine_allows(fake_store, ns):
    """R-PLAN-8: the default bound and every widened bound stay within MAX_ROUTE_STEPS, whatever
    cap the caller passes; an 8-step route is found and a 9-step one is reported as out of reach."""
    spy = BoundSpy(fake_store)
    eight = chain(fake_store, ns, 8)
    request = PlanRequest(site=SITE, state_id=state_id_of(fake_store, ns, eight[0]), goal_text=GOAL)
    assert len(plan(spy, ns, request).route) == 8
    assert len(plan(spy, ns, request, max_steps=3, max_steps_cap=48).route) == 8

    learn(fake_store, ns, page("/step/before", "button:go-first"), click("go-first"), eight[0])
    far = PlanRequest(
        site=SITE,
        state_id=state_id_of(fake_store, ns, page("/step/before", "button:go-first")),
        goal_text=GOAL,
    )
    result = plan(spy, ns, far, max_steps=3, max_steps_cap=48)
    assert (result.route, result.reason) == ((), PlanReason.NO_ROUTE_WITHIN_MAX_STEPS)
    assert result.max_steps_used == MAX_ROUTE_STEPS
    assert spy.bounds and max(spy.bounds) == MAX_ROUTE_STEPS


def search_then_finish(store: GraphStore, ns: str, order_id: str) -> None:
    listing = page("/orders", "textbox:Search orders", "button:Search", rows=3)
    results = page(f"/orders?q={order_id}", "textbox:Search orders", "button:Search", rows=1)
    search = Action(
        kind=ActionKind.CLICK,
        locator=Locator(role="button", name="Search"),
        fills=(Fill(locator=Locator(role="textbox", name="Search orders"), value=order_id),),
    )
    learn(
        store,
        ns,
        listing,
        search,
        results,
        goal_text=f"find order {order_id}",
        goal_reached=True,
        params={"order_id": order_id},
    )


def test_routes_generalise_over_task_parameters(fake_store, ns):
    """R-PARAM-1: a route learned with one order id is served, with slots, for another."""
    search_then_finish(fake_store, ns, "1042")
    listing = page("/orders", "textbox:Search orders", "button:Search", rows=5)
    result = plan_from(fake_store, ns, listing, goal="find order 77", params={"order_id": "77"})
    assert result.reason is PlanReason.ROUTE_FOUND
    fill = result.route[0].action.fills[0]
    assert (fill.param, fill.value) == ("order_id", None)
    bound = bind_action(result.route[0].action, {"order_id": "77"})
    assert (bound.fills[0].param, bound.fills[0].value) == (None, "77")


def test_route_needing_a_missing_parameter_is_not_served(fake_store, ns):
    """R-PLAN-5: a route whose slots the caller cannot fill is reported, not returned."""
    search_then_finish(fake_store, ns, "1042")
    listing = page("/orders", "textbox:Search orders", "button:Search", rows=5)
    result = plan_from(fake_store, ns, listing, goal="find order {order_id}")
    assert result.route == ()
    assert result.needs_model is True
    assert result.reason is PlanReason.MISSING_PARAMS


def test_planning_from_an_unknown_state_is_an_error(fake_store, ns):
    """R-PLAN-6: a state id the map has never seen raises UnknownStateError."""
    build_map(fake_store, ns)
    request = PlanRequest(site=SITE, state_id=123456789, goal_text=GOAL)
    with pytest.raises(UnknownStateError):
        plan(fake_store, ns, request)
