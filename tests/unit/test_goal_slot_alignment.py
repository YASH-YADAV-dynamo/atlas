"""One-slot alignment (SHIP_PLAN 4.3 A4, section 5): a goal text with no params that equals a stored
one-slot goal except for one span of 1 to 6 tokens binds that span to the slot. Automatic for read
goals; offered as `match: "similar"` with the inferred `params` for act goals.
Targets atlas.core.goals, the planner and MCP atlas_plan."""

from __future__ import annotations

from atlas.contracts import mcp_models as m
from atlas.contracts.api_models import PlanRequest
from atlas.contracts.types import (
    Action,
    ActionKind,
    Fill,
    GoalKind,
    Locator,
    Params,
    PlanReason,
    PlanResult,
    ReportEvent,
)
from atlas.core.learner import report
from atlas.core.planner import plan
from atlas.mcp.tools import AtlasTools
from tests.support.builders import SITE, FixedClock, new_event_id, page, state_id_of

HOME = page("/", "searchbox:Order number", "link:Help")
DONE = page("/orders/1042", "button:Refund")
BOX = Locator(role="searchbox", name="Order number")


def teach(store, ns, goal_text: str, params: Params, *, kind: GoalKind = "act", value: str = "1042"):
    from atlas.core.locate import locate

    action = Action(
        kind=ActionKind.PRESS, locator=BOX, value="Enter", fills=(Fill(locator=BOX, value=value),)
    )
    event = ReportEvent(
        site=SITE,
        from_state=locate(store, ns, SITE, HOME).state_id,
        action=action,
        to_snapshot=DONE,
        ok=True,
        ms=50,
        event_id=new_event_id(),
        goal_text=goal_text,
        params=params,
        goal_reached=True,
        kind=kind,
    )
    return report(store, FixedClock(), ns, event)


def ask(store, ns, goal_text: str, params: Params | None = None) -> PlanResult:
    request = PlanRequest(
        site=SITE, state_id=state_id_of(store, ns, HOME), goal_text=goal_text, params=params or {}
    )
    return plan(store, ns, request)


def test_free_form_act_goal_is_offered_with_the_bound_value(fake_store, ns):
    teach(fake_store, ns, "Refund order 1042", {"order_id": "1042"})
    result = ask(fake_store, ns, "please refund order 1005 for me")
    assert (result.needs_model, result.reason, result.route) == (True, PlanReason.UNKNOWN_GOAL, ())
    assert result.match == "similar"
    assert result.goal == "refund order {order_id}"
    assert result.params == {"order_id": "1005"}
    confirmed = ask(fake_store, ns, result.goal, result.params)
    assert confirmed.reason is PlanReason.ROUTE_FOUND and confirmed.match == "exact"


def test_read_goal_is_bound_and_routed_automatically(fake_store, ns):
    teach(fake_store, ns, "Look up order 1042 status", {"order_id": "1042"}, kind="read")
    result = ask(fake_store, ns, "please look up the order AB-77 status")
    assert result.reason is PlanReason.ROUTE_FOUND and result.route
    assert result.match == "same_words"
    assert result.goal == "look up order {order_id} status"
    assert result.params == {"order_id": "AB-77"}


def test_bound_value_keeps_its_original_spelling_and_inner_words(fake_store, ns):
    teach(fake_store, ns, "Open the Wikipedia article about Kajol", {"topic": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "open the wikipedia article about Hrithik Roshan please")
    assert result.params == {"topic": "Hrithik Roshan"}
    result = ask(fake_store, ns, "Open the Wikipedia article about The Lord of the Rings")
    assert result.params == {"topic": "Lord of the Rings"}  # leading stopword is not content


def test_span_longer_than_six_tokens_is_not_bound(fake_store, ns):
    teach(fake_store, ns, "Refund order 1042", {"order_id": "1042"})
    result = ask(fake_store, ns, "refund order one two three four five six seven")
    assert result.params is None and result.match is None


def test_multi_slot_goals_are_not_aligned(fake_store, ns):
    teach(fake_store, ns, "set product A1 price to 5", {"sku": "A1", "price": "5"})
    result = ask(fake_store, ns, "set product B2 price to 9")
    assert (result.match, result.params) == (None, None)


def test_no_alignment_when_params_are_given(fake_store, ns):
    teach(fake_store, ns, "Refund order 1042", {"order_id": "1042"})
    result = ask(fake_store, ns, "please refund order 1005 for me", {"order_id": "1005"})
    assert result.params is None  # the caller's params stand; the wording is matched on its own


def test_two_goals_binding_different_values_bind_nothing(fake_store, ns):
    teach(fake_store, ns, "Refund order 1042", {"order_id": "1042"})
    teach(fake_store, ns, "Refund order 1042 now", {"note": "1042 now"}, value="x")
    result = ask(fake_store, ns, "refund order 1005 now")  # binds order_id in one, note in the other
    assert (result.route, result.params, result.match) == ((), None, None)


def test_one_anchor_word_is_not_enough(fake_store, ns):
    teach(fake_store, ns, "open order 42", {"order_id": "42"}, kind="read")
    result = ask(fake_store, ns, "show orders from 2024")
    assert (result.route, result.params) == ((), None)


def test_mcp_plan_returns_goal_match_and_params(fake_store, ns):
    teach(fake_store, ns, "Refund order 1042", {"order_id": "1042"})
    tools = AtlasTools(fake_store, FixedClock(), ns, SITE, None)
    out = tools.plan(
        m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text="please refund order 1005 for me")
    )
    assert out.needs_model and out.route == ()
    assert (out.match, out.goal, out.params) == ("similar", "refund order {order_id}", {"order_id": "1005"})
    accepted = tools.plan(
        m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=out.goal, params=out.params)
    )
    assert accepted.reason is PlanReason.ROUTE_FOUND and accepted.match == "exact"
