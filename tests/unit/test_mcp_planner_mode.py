"""MCP planner mode (wave 6): the agent drives its own browser and uses Atlas to plan and learn.

Targets atlas.mcp.tools.AtlasTools(store, clock, ns, site, None)
    .plan(PlanToolInput) -> PlanToolOutput      .report(ReportToolInput) -> ReportToolOutput
In these tests the "agent's own browser" is tests/support/fake_site.py and the agent's
brain is the OracleModel; Atlas never touches the browser.
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.mcp.tools", reason="MCP wave: implemented by lane M")

from atlas.contracts import mcp_models as m
from atlas.contracts.types import PlanReason
from atlas.core.params import bind_action
from atlas.mcp.tools import AtlasTools
from tests.support.builders import SITE, FixedClock
from tests.support.fake_site import FakeSite
from tests.support.oracle import OracleModel

if not hasattr(AtlasTools, "plan"):
    pytest.skip("planner mode (increment 6) not implemented yet", allow_module_level=True)


def checked(result):
    assert m.response_tokens(result) <= m.MCP_RESPONSE_TOKEN_BUDGET
    return result


def plan(tools: AtlasTools, browser: FakeSite, order: str) -> m.PlanToolOutput:
    args = m.PlanToolInput(
        site=SITE,
        snapshot_summary=browser.snapshot(),
        goal_text=f"Refund order {order}",
        params={"order_id": order},
    )
    return checked(tools.plan(args))


def explore_and_report(tools: AtlasTools, order: str) -> list[m.ReportToolOutput]:
    """An agent with its own browser does the task by itself and reports every step."""
    browser, brain, params = FakeSite(), OracleModel(), {"order_id": order}
    state = plan(tools, browser, order).state_id
    reports: list[tuple[m.ReportToolInput, m.ReportToolOutput]] = []
    while True:
        decision = brain.next_action(f"Refund order {order}", params, browser.snapshot(), ())
        if decision.done:
            break
        browser.act(decision.action)
        args = m.ReportToolInput(
            site=SITE,
            from_state=state,
            action=decision.action,
            to_snapshot_summary=browser.snapshot(),
            ok=True,
            goal_text=f"Refund order {order}",
            params=params,
        )
        out = checked(tools.report(args))
        reports.append((args, out))
        state = out.state_id
    last_args, last_out = reports[-1]
    linked = tools.report(last_args.model_copy(update={"event_id": last_out.event_id, "goal_reached": True}))
    assert (linked.duplicate, linked.event_id, linked.state_id) == (
        True,
        last_out.event_id,
        last_out.state_id,
    )
    return [out for _, out in reports]


def test_planner_mode_round_trip(fake_store, ns):
    """R-MCP-6: plan on an empty map says needs_model; after the agent reports its own steps,
    the plan for a different order is the full route with slots, and following it works."""
    tools = AtlasTools(fake_store, FixedClock(), ns, SITE, None)
    empty = plan(tools, FakeSite(), "1042")
    assert (empty.route, empty.needs_model, empty.reason) == ((), True, PlanReason.UNKNOWN_GOAL)

    reports = explore_and_report(tools, "1042")
    assert len(reports) == 5
    assert all(not out.duplicate for out in reports)
    assert len({out.event_id for out in reports}) == 5

    browser = FakeSite()
    route = plan(tools, browser, "77")
    assert (route.needs_model, route.reason, len(route.route)) == (False, PlanReason.ROUTE_FOUND, 5)
    assert route.state_id == empty.state_id
    assert route.route[1].action.fills[0].param == "order_id"
    assert all(0 < step.reliability < 1 for step in route.route)
    state = route.state_id
    for step in route.route:
        browser.act(bind_action(step.action, {"order_id": "77"}))
        args = m.ReportToolInput(
            site=SITE,
            from_state=state,
            action=step.action,
            to_snapshot_summary=browser.snapshot(),
            ok=True,
            expect_state=step.expect,
            params={"order_id": "77"},
        )
        out = checked(tools.report(args))
        assert (out.state_id, out.known) == (step.expect, True)
        state = out.state_id
    assert browser.refunded == {"77"}
    counts = sorted((t.ok, t.fail) for t in fake_store.get_map(ns, SITE).transitions)
    assert counts == [(2, 0)] * 5


def test_planner_report_is_idempotent_on_its_event_id(fake_store, ns):
    """R-MCP-6, R-LEARN-3: a report without event_id gets one and echoes it; reusing it is a
    duplicate; two reports without ids are two events."""
    tools = AtlasTools(fake_store, FixedClock(), ns, SITE, None)
    browser = FakeSite()
    start = plan(tools, browser, "5").state_id
    action = OracleModel().next_action("Refund order 5", {"order_id": "5"}, browser.snapshot(), ()).action
    browser.act(action)
    args = m.ReportToolInput(
        site=SITE, from_state=start, action=action, to_snapshot_summary=browser.snapshot(), ok=True
    )
    first = tools.report(args)
    second = tools.report(args)
    retry = tools.report(args.model_copy(update={"event_id": first.event_id}))
    assert first.event_id != second.event_id
    assert (first.duplicate, second.duplicate, retry.duplicate) == (False, False, True)
    assert [(t.ok, t.fail) for t in fake_store.get_map(ns, SITE).transitions] == [(2, 0)]
