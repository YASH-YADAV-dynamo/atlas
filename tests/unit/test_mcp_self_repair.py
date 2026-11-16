"""Self-repair in MCP runner mode (SPEC 4.4): a mapped step that fails or lands elsewhere is
penalised on exactly that transition, the run replans from where the browser really is,
and the agent is only asked for help where the map truly ends.

Targets atlas.mcp.tools.AtlasTools.run / .act. Rule added by RCA 2026-11-15 (failed mapped
step): within one call a transition that just failed is not retried; if the replanned route
would start with it, the call stops at the frontier with `needs_choice`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.mcp.tools", reason="MCP wave: implemented by lane M")

from atlas.contracts import mcp_models as m
from atlas.mcp.tools import AtlasTools
from tests.support.builders import SITE, FixedClock
from tests.support.fake_site import FakeSite
from tests.unit.test_mcp_tools import act, checked, teach_refund


def run(store, ns, site: FakeSite, order: str) -> tuple[AtlasTools, m.RunOutput]:
    tools = AtlasTools(store, FixedClock(), ns, SITE, site)
    goal = m.RunInput(goal_text=f"Refund order {order}", params={"order_id": order})
    return tools, checked(tools.run(goal))


def counts(store, ns, name: str) -> list[tuple[int, int]]:
    """(ok, fail) of every transition whose trigger is the control `name`."""
    transitions = store.get_map(ns, SITE).transitions
    return sorted((t.ok, t.fail) for t in transitions if t.action.locator and t.action.locator.name == name)


def test_dead_control_is_penalised_and_the_agent_is_asked(fake_store, ns):
    """R-REPLAN-1, R-MCP-9: a mapped step that cannot execute records one failure on that
    transition (never a bare 'failed' with nothing learned), and the call ends at the
    frontier instead of retrying the same step."""
    teach_refund(fake_store, ns, "1042")
    site = FakeSite(dead=frozenset({"Confirm refund"}))
    _, out = run(fake_store, ns, site, "77")
    assert out.status is m.RunStatus.NEEDS_CHOICE
    assert (out.steps_driven, site.acts, site.page) == (4, 4, "confirm")
    assert {"Confirm refund", "Cancel"} <= {c.name for c in out.choices}
    assert counts(fake_store, ns, "Confirm refund") == [(1, 1)]
    assert counts(fake_store, ns, "Refund order") == [(2, 0)]
    assert site.refunded == set()


def test_renamed_control_reroutes_after_one_explored_step(fake_store, ns):
    """R-REPLAN-1, R-MUT-1 (MCP level): after a control is renamed, the run drives as far as the
    map is still true, records the old road's failure and the page it actually reached, asks
    for one choice, then finishes from the map; the next run needs no help."""
    teach_refund(fake_store, ns, "1042")
    changed = {"Refund order": "Issue refund"}

    site = FakeSite(renamed=changed)
    tools, stuck = run(fake_store, ns, site, "77")
    assert stuck.status is m.RunStatus.NEEDS_CHOICE
    assert (stuck.steps_driven, site.page) == (3, "detail")
    assert "Issue refund" in {c.name for c in stuck.choices}
    assert counts(fake_store, ns, "View order") == [(1, 0), (1, 1)]

    done = act(tools, stuck, "Issue refund")
    assert (done.status, done.steps_driven) == (m.RunStatus.DONE, 1)
    assert site.refunded == {"77"}

    again = FakeSite(renamed=changed)
    _, straight = run(fake_store, ns, again, "88")
    assert (straight.status, straight.steps_driven) == (m.RunStatus.DONE, 5)
    assert again.refunded == {"88"}


def test_old_and_new_variants_of_the_site_both_keep_working(fake_store, ns):
    """R-REPLAN-1: once both variants are on the map, a run on either one finishes in one call,
    repairing its route on the way if the cheaper road belongs to the other variant."""
    teach_refund(fake_store, ns, "1042")
    changed = {"Refund order": "Issue refund"}
    site = FakeSite(renamed=changed)
    tools, stuck = run(fake_store, ns, site, "77")
    act(tools, stuck, "Issue refund")

    for order, variant in (("201", FakeSite()), ("202", FakeSite(renamed=changed)), ("203", FakeSite())):
        _, out = run(fake_store, ns, variant, order)
        assert out.status is m.RunStatus.DONE, order
        assert out.steps_driven == 5
        assert variant.refunded == {order}
