"""First-step guard (SHIP_PLAN 4.1 guard 4, 4.3 list-only pages): when the plan request carries a
snapshot with elements, a route is returned only if its first step's control is on that page.
Targets atlas.core.planner.plan."""

from __future__ import annotations

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.types import Action, ActionKind, Fill, Locator, PlanReason, Snapshot
from atlas.core.planner import control_on_page, plan
from tests.support.builders import GOAL, SITE, click, learn, page, state_id_of

HOME = page("/", "link:Orders", "link:Settings")
ORDERS = page("/orders", "link:Dashboard")


def request(store, ns, snapshot: Snapshot | None, **params: str) -> PlanRequest:
    return PlanRequest(
        site=SITE, state_id=state_id_of(store, ns, HOME), goal_text=GOAL, params=params, snapshot=snapshot
    )


def teach(store, ns) -> None:
    learn(store, ns, HOME, click("Orders", "link"), ORDERS, goal_text=GOAL, goal_reached=True)


def test_route_when_the_first_control_is_on_the_page(fake_store, ns):
    teach(fake_store, ns)
    result = plan(fake_store, ns, request(fake_store, ns, HOME))
    assert result.reason is PlanReason.ROUTE_FOUND


def test_no_route_when_the_first_control_is_missing(fake_store, ns):
    teach(fake_store, ns)
    merged = page("/", "link:Settings", "link:Help")  # a different page recognised as HOME
    result = plan(fake_store, ns, request(fake_store, ns, merged))
    assert (result.needs_model, result.reason) == (True, PlanReason.NO_ROUTE_WITHIN_MAX_STEPS)


def test_snapshots_without_elements_and_old_requests_are_not_judged(fake_store, ns):
    teach(fake_store, ns)
    light = Snapshot(url=HOME.url, title="Home")
    assert plan(fake_store, ns, request(fake_store, ns, light)).reason is PlanReason.ROUTE_FOUND
    assert plan(fake_store, ns, request(fake_store, ns, None)).reason is PlanReason.ROUTE_FOUND


def test_names_match_case_insensitively_after_binding_params():
    snapshot = page("/", "link:Hrithik Roshan - Wikipedia", "searchbox:Search Wikipedia")
    pick = Action(kind=ActionKind.CLICK, locator=Locator(role="link", name="{person} - wikipedia"))
    assert control_on_page(pick, snapshot, {"person": "hrithik roshan"})
    assert not control_on_page(pick, snapshot, {"person": "Kajol"})
    search = Action(
        kind=ActionKind.PRESS,
        locator=Locator(role="searchbox", name="Search Wikipedia"),
        value="Enter",
        fills=(Fill(locator=Locator(role="searchbox", name="Search Wikipedia"), param="person"),),
    )
    assert control_on_page(search, snapshot, {"person": "Kajol"})
    assert not control_on_page(click("Search Wikipedia", "button"), snapshot, {})
    assert control_on_page(Action(kind=ActionKind.GOTO, value="/x"), snapshot, {})
