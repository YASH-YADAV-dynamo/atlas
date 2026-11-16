"""MCP runner mode on the in-memory store and a scripted site (tests/support/fake_site.py).

Targets (lane M):
    atlas.mcp.tools.AtlasTools(store: MapStore, clock: Clock, ns: str, site: str,
                               driver: BrowserDriver | None = None, *, max_steps: int = 25)
        .find_skills(FindSkillsInput) -> FindSkillsOutput
        .run(RunInput) -> RunOutput          .act(ActInput) -> RunOutput
        .recall(RecallInput) -> RecallOutput
A new AtlasTools is a new agent session; the store is the shared map. Every result must
already be within the response token budget.
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.mcp.tools", reason="MCP wave: implemented by lane M")

from atlas.contracts import mcp_models as m
from atlas.contracts.errors import ToolUnavailableError
from atlas.mcp.tools import AtlasTools
from tests.support.builders import SITE, FixedClock
from tests.support.fake_site import FakeSite

REFUND = "Refund order {}"


def session(store, ns, site: FakeSite | None) -> AtlasTools:
    return AtlasTools(store, FixedClock(), ns, SITE, site)


def checked(result):
    assert m.response_tokens(result) <= m.MCP_RESPONSE_TOKEN_BUDGET
    return result


def pick(result: m.RunOutput, name: str) -> int:
    matches = [c.id for c in result.choices if c.name == name]
    assert len(matches) == 1, (name, result.choices)
    return matches[0]


def act(tools: AtlasTools, previous: m.RunOutput, name: str, **extra) -> m.RunOutput:
    return checked(tools.act(m.ActInput(action=pick(previous, name), **extra)))


def teach_refund(store, ns, order_id: str = "1042") -> FakeSite:
    """One agent session that explores the whole refund flow through atlas_run / atlas_act."""
    site = FakeSite()
    tools = session(store, ns, site)
    out = checked(tools.run(m.RunInput(goal_text=REFUND.format(order_id), params={"order_id": order_id})))
    assert (out.status, out.steps_driven) == (m.RunStatus.NEEDS_CHOICE, 0)
    out = act(tools, out, "Orders")
    out = act(tools, out, "Search orders", value=order_id)
    out = act(tools, out, "Search")
    out = act(tools, out, "View order", row=order_id)
    out = act(tools, out, "Refund order")
    out = act(tools, out, "Confirm refund")
    assert out.status is m.RunStatus.NEEDS_CHOICE
    done = checked(tools.act(m.ActInput(goal_reached=True)))
    assert done.status is m.RunStatus.DONE
    assert site.refunded == {order_id}
    return site


def test_empty_map_stops_at_the_first_page_with_its_controls(fake_store, ns):
    """R-MCP-3: with no route, atlas_run returns needs_choice and only role/name choices."""
    site = FakeSite()
    out = checked(
        session(fake_store, ns, site).run(m.RunInput(goal_text="Refund order 7", params={"order_id": "7"}))
    )
    assert (out.status, out.steps_driven, out.model_calls_saved) == (m.RunStatus.NEEDS_CHOICE, 0, 0)
    assert [(c.role, c.name) for c in out.choices] == [("link", "Orders"), ("link", "Settings")]
    assert [c.id for c in out.choices] == [0, 1]
    assert site.acts == 0


def test_form_values_are_fills_not_steps_and_list_rows_collapse(fake_store, ns):
    """R-MCP-3: a value given to an input is remembered and sent with the next trigger; repeated
    row controls are offered once, flagged `repeated`."""
    site = FakeSite()
    tools = session(fake_store, ns, site)
    out = checked(tools.run(m.RunInput(goal_text="Refund order 1042", params={"order_id": "1042"})))
    orders = act(tools, out, "Orders")
    view = [c for c in orders.choices if c.name == "View order"]
    assert len(view) == 1 and view[0].repeated is True
    filled = act(tools, orders, "Search orders", value="1042")
    assert filled.status is m.RunStatus.NEEDS_CHOICE
    assert (site.acts, site.typed) == (1, [])
    assert len(fake_store.get_map(ns, SITE).transitions) == 1
    act(tools, filled, "Search")
    assert (site.acts, site.typed, site.query) == (2, ["1042"], "1042")
    assert len(fake_store.get_map(ns, SITE).transitions) == 2


def test_learned_task_runs_in_one_call_for_new_parameters(fake_store, ns):
    """R-MCP-3, R-PARAM-1: after one explored run, one atlas_run call drives the whole task
    for a different order with no further round trips."""
    teach_refund(fake_store, ns, "1042")
    site = FakeSite()
    out = checked(
        session(fake_store, ns, site).run(m.RunInput(goal_text="Refund order 77", params={"order_id": "77"}))
    )
    assert out.status is m.RunStatus.DONE
    assert (out.steps_driven, out.model_calls_saved) == (5, 5)
    assert out.choices == ()
    assert out.result_summary
    assert (site.refunded, site.typed, site.acts) == ({"77"}, ["77"], 5)
    assert m.EntityRef(type="order_id", key="77") in out.entities_resolved


def test_run_resumes_driving_once_the_route_becomes_known(fake_store, ns):
    """R-MCP-4: from a page with no known route, one atlas_act step that rejoins the map is
    learned and the rest is driven in the same call."""
    teach_refund(fake_store, ns, "1042")
    site = FakeSite(start="settings")
    tools = session(fake_store, ns, site)
    stuck = checked(tools.run(m.RunInput(goal_text="Refund order 88", params={"order_id": "88"})))
    assert (stuck.status, stuck.steps_driven) == (m.RunStatus.NEEDS_CHOICE, 0)
    assert site.acts == 0
    out = act(tools, stuck, "Orders")
    assert out.status is m.RunStatus.DONE
    assert (out.steps_driven, out.model_calls_saved) == (4, 4)
    assert site.refunded == {"88"}

    again = FakeSite(start="settings")
    direct = checked(
        session(fake_store, ns, again).run(m.RunInput(goal_text="Refund order 99", params={"order_id": "99"}))
    )
    assert (direct.status, direct.steps_driven) == (m.RunStatus.DONE, 5)
    assert again.refunded == {"99"}


def test_find_skills_reads_goals_from_the_graph_and_feeds_run(fake_store, ns):
    """R-MCP-5: learned goals are served as skills with their parameter names, best match first,
    and a returned skill_id runs."""
    teach_refund(fake_store, ns, "1042")
    site = FakeSite()
    tools = session(fake_store, ns, site)
    found = checked(tools.find_skills(m.FindSkillsInput(site=SITE, goal_text="please refund order 55")))
    assert 1 <= len(found.skills) <= m.MAX_SKILLS
    best = found.skills[0]
    assert best.name == "refund order {order_id}"
    assert best.params == ("order_id",)
    assert 0 < best.reliability < 1
    assert (
        session(fake_store, ns, None)
        .find_skills(m.FindSkillsInput(site="other.test", goal_text="refund"))
        .skills
        == ()
    )

    out = checked(tools.run(m.RunInput(skill_id=best.skill_id, params={"order_id": "55"})))
    assert (out.status, out.steps_driven) == (m.RunStatus.DONE, 5)
    assert site.refunded == {"55"}


def test_recall_returns_where_an_entity_was_found_and_the_route(fake_store, ns):
    """R-ENT-1: an entity used in a run is stored with the page it led to and can be recalled
    with a bound route from the current page."""
    teach_refund(fake_store, ns, "1042")
    tools = session(fake_store, ns, FakeSite())
    hit = checked(tools.recall(m.RecallInput(entity_type="order_id", key="1042")))
    assert hit.found is True
    assert hit.page == "/orders/{}"
    assert hit.needs_model is False
    assert [step.action.locator.name for step in hit.route] == ["Orders", "Search", "View order"]
    assert hit.route[1].action.fills[0].value == "1042"
    assert hit.route[-1].expect == hit.state_id

    miss = checked(tools.recall(m.RecallInput(entity_type="order_id", key="31337")))
    assert (miss.found, miss.route, miss.state_id) == (False, (), None)

    no_browser = checked(
        session(fake_store, ns, None).recall(m.RecallInput(entity_type="order_id", key="1042"))
    )
    assert (no_browser.found, no_browser.route, no_browser.page) == (True, (), "/orders/{}")


def test_bad_tool_use_fails_short_and_changes_nothing(fake_store, ns):
    """R-MCP-7: errors are short and actionable; an invalid step touches neither browser nor map."""
    site = FakeSite()
    tools = session(fake_store, ns, site)
    early = checked(tools.act(m.ActInput(action=0)))
    assert early.status is m.RunStatus.FAILED and early.message
    out = checked(tools.run(m.RunInput(goal_text="Refund order 7", params={"order_id": "7"})))
    wrong = checked(tools.act(m.ActInput(action=len(out.choices))))
    assert wrong.status is m.RunStatus.FAILED and wrong.message
    assert site.acts == 0
    assert fake_store.get_map(ns, SITE).transitions == ()
    unknown = checked(tools.run(m.RunInput(skill_id="goal:123", params={})))
    assert unknown.status is m.RunStatus.FAILED and unknown.message


def test_runner_tools_need_a_browser(fake_store, ns):
    """R-MCP-7: atlas_run and atlas_act in planner mode raise ToolUnavailableError."""
    tools = session(fake_store, ns, None)
    with pytest.raises(ToolUnavailableError):
        tools.run(m.RunInput(goal_text="Refund order 7"))
    with pytest.raises(ToolUnavailableError):
        tools.act(m.ActInput(action=0))


def test_large_pages_are_cut_to_the_budget(fake_store, ns):
    """R-MCP-2: a page with far more controls than fit still yields a response within budget."""
    site = FakeSite(extra_controls=80)
    out = session(fake_store, ns, site).run(m.RunInput(goal_text="Refund order 7", params={"order_id": "7"}))
    assert m.response_tokens(out) <= m.MCP_RESPONSE_TOKEN_BUDGET
    assert out.truncated is True
    assert 1 <= len(out.choices) <= m.MAX_CHOICES
    assert [(c.role, c.name) for c in out.choices[:2]] == [("link", "Orders"), ("link", "Settings")]
    assert all(len(c.name) <= m.CHOICE_NAME_LENGTH for c in out.choices)
