"""Planner mode: a fill reported on its own is folded into the action that submits it.

Real agents report "type into the search box" and "click Search" as two steps. Stored as a
self-loop the fill would never be part of a planned route, and a replay would search for nothing.
"""

from __future__ import annotations

from atlas.adapters.cached import CachedStore
from atlas.adapters.hydradb.fake import InMemoryGraphStore
from atlas.contracts import mcp_models as m
from atlas.contracts.types import ActionKind
from atlas.mcp.pending import PENDING
from atlas.mcp.tools import AtlasTools
from tests.support.builders import FixedClock

SITE = "shop"
GOAL = "Search for {q} and open the first result"


def snap(url: str, title: str, *els: tuple[str, str]) -> dict[str, object]:
    return {"url": url, "title": title, "elements": [{"role": r, "name": n} for r, n in els]}


HOME = snap("https://shop.test/", "Home", ("searchbox", "Search"), ("button", "Go"))
RESULTS = snap("https://shop.test/search", "Results", ("link", "Item A"), ("link", "Item B"))
ITEM = snap("https://shop.test/item/1", "Item", ("button", "Add to cart"))
FILL = {"kind": "fill", "locator": {"role": "searchbox", "name": "Search"}, "value": "desk lamp"}
GO = {"kind": "click", "locator": {"role": "button", "name": "Go"}}
OPEN = {"kind": "click", "locator": {"role": "link", "name": "Item A"}}


def search_once(tools: AtlasTools, query: str) -> m.PlanToolOutput:
    params = {"q": query}
    first = tools.plan(m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=GOAL, params=params))
    state = first.state_id
    fill = {**FILL, "value": query}
    for i, (action, to) in enumerate([(fill, HOME), (GO, RESULTS), (OPEN, ITEM)]):
        out = tools.report(
            m.ReportToolInput(
                site=SITE,
                from_state=state,
                action=action,
                to_snapshot_summary=to,
                ok=True,
                ms=50,
                goal_text=GOAL if i == 2 else None,
                params=params,
                goal_reached=i == 2,
            )
        )
        state = out.state_id
    return first


def replan(tools: AtlasTools, query: str) -> m.PlanToolOutput:
    return tools.plan(m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=GOAL, params={"q": query}))


def test_fill_then_click_replays_with_the_fill(ns):
    tools = AtlasTools(InMemoryGraphStore(), FixedClock(), ns, SITE, None)
    assert search_once(tools, "desk lamp").needs_model
    again = replan(tools, "kettle")
    assert not again.needs_model
    go = again.route[0].action
    assert (go.kind, go.locator and go.locator.name) == (ActionKind.CLICK, "Go")
    assert [(f.locator.role, f.param, f.value) for f in go.fills] == [("searchbox", "q", None)]
    # no self-loop fill transition was stored
    transitions = tools.store.get_map(ns, SITE).transitions
    assert all(t.from_state != t.to_state for t in transitions)


def test_standalone_fill_report_returns_the_same_page(ns):
    tools = AtlasTools(InMemoryGraphStore(), FixedClock(), ns, SITE, None)
    state = tools.plan(m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=GOAL)).state_id
    out = tools.report(
        m.ReportToolInput(site=SITE, from_state=state, action=FILL, to_snapshot_summary=HOME, ok=True)
    )
    assert (out.state_id, out.duplicate) == (state, False)
    assert out.event_id
    PENDING.take((ns, SITE, state, ""))  # leave no pending state behind for other tests


def test_failed_click_keeps_the_pending_fill(ns):
    tools = AtlasTools(InMemoryGraphStore(), FixedClock(), ns, SITE, None)
    state = tools.plan(m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=GOAL)).state_id
    report = lambda action, to, ok: tools.report(  # noqa: E731
        m.ReportToolInput(site=SITE, from_state=state, action=action, to_snapshot_summary=to, ok=ok)
    )
    report(FILL, HOME, True)
    report(GO, HOME, False)
    report(GO, RESULTS, True)
    clicks = [t for t in tools.store.get_map(ns, SITE).transitions if t.ok]
    assert len(clicks) == 1 and clicks[0].action.fills[0].locator.role == "searchbox"


def test_works_through_the_cached_store(ns):
    tools = AtlasTools(CachedStore(InMemoryGraphStore()), FixedClock(), ns, SITE, None)
    search_once(tools, "desk lamp")
    for query in ("kettle", "kettle", "rug"):
        again = replan(tools, query)
        assert not again.needs_model and len(again.route) == 2
