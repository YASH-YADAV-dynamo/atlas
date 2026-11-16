"""D13.4: atlas_find_skills with no goal_text lists goals; with no site it searches every site of the key."""

from __future__ import annotations

from atlas.contracts import mcp_models as m
from atlas.mcp.tools import AtlasTools
from tests.support.builders import FixedClock
from tests.support.mcp_call import call_tool
from tests.unit.test_mcp_plan_replay import GOAL, SITE, teach, tools  # noqa: F401


def test_no_goal_text_lists_the_known_goals(tools):  # noqa: F811
    teach(tools)
    out = call_tool(tools, "atlas_find_skills", {"site": SITE})
    assert [s.name for s in out.skills] == [GOAL.lower()] and out.skills[0].site is None


def test_no_site_searches_every_site_of_the_key(tools):  # noqa: F811
    teach(tools)
    out = call_tool(tools, "atlas_find_skills", {"goal_text": "open the article about {topic}"})
    assert [(s.name, s.site) for s in out.skills] == [(GOAL.lower(), SITE)]
    both = call_tool(tools, "atlas_find_skills", {})
    assert [s.site for s in both.skills] == [SITE]
    assert m.response_tokens(both) <= m.MCP_RESPONSE_TOKEN_BUDGET


def test_nothing_known_is_an_empty_list(tools):  # noqa: F811
    assert call_tool(tools, "atlas_find_skills", {}).skills == ()


def test_tenancy_is_unchanged(fake_store, ns, tools):  # noqa: F811
    teach(tools)
    stranger = AtlasTools(fake_store, FixedClock(), ns + "_other", SITE)
    assert call_tool(stranger, "atlas_find_skills", {}).skills == ()
    assert call_tool(stranger, "atlas_find_skills", {"goal_text": GOAL}).skills == ()


def test_the_most_reliable_goal_comes_first_without_a_goal_text(tools):  # noqa: F811
    teach(tools)
    out = call_tool(tools, "atlas_find_skills", {})
    rates = [s.reliability for s in out.skills]
    assert rates == sorted(rates, reverse=True)
