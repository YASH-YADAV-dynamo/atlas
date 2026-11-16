"""find_skills ranks on content words only (SHIP_PLAN 4.3 A5): stopwords and retrieval verbs do not
count, a goal must share at least one content word and at least half of the query's, and `~` alias
goals are never listed. Targets atlas.mcp.tools.AtlasTools.find_skills and atlas.core.goals."""

from __future__ import annotations

from atlas.contracts import mcp_models as m
from atlas.contracts.types import GoalKind, Params, ReportEvent
from atlas.core.goals import skill_match
from atlas.core.learner import report
from atlas.mcp.tools import AtlasTools
from tests.support.builders import SITE, FixedClock, click, new_event_id, page

HOME = page("/", "link:Orders", "link:Products", "link:Settings")


def teach(store, ns, goal_text: str, params: Params, control: str, *, kind: GoalKind = "act") -> None:
    from atlas.core.locate import locate

    event = ReportEvent(
        site=SITE,
        from_state=locate(store, ns, SITE, HOME).state_id,
        action=click(control, "link"),
        to_snapshot=page(f"/{control.lower()}", "link:Back"),
        ok=True,
        ms=50,
        event_id=new_event_id(),
        goal_text=goal_text,
        params=params,
        goal_reached=True,
        kind=kind,
    )
    report(store, FixedClock(), ns, event)


def names(store, ns, query: str) -> list[str]:
    tools = AtlasTools(store, FixedClock(), ns, SITE, None)
    return [s.name for s in tools.find_skills(m.FindSkillsInput(site=SITE, goal_text=query)).skills]


def seed(store, ns) -> None:
    teach(store, ns, "set product A1 price to 5", {"sku": "A1", "price": "5"}, "Products")
    teach(store, ns, "refund order 1042", {"order_id": "1042"}, "Orders")
    teach(store, ns, "enable dark mode", {}, "Settings", kind="read")


def test_unrelated_query_returns_nothing_bug_a5(fake_store, ns):
    seed(fake_store, ns)
    assert names(fake_store, ns, "I need to give a customer their money back") == []


def test_best_content_match_first(fake_store, ns):
    seed(fake_store, ns)
    assert names(fake_store, ns, "please refund order 55")[0] == "refund order {order_id}"
    assert names(fake_store, ns, "change the price of a product")[0] == "set product {sku} price to {price}"


def test_needs_half_of_the_query_words(fake_store, ns):
    seed(fake_store, ns)
    assert names(fake_store, ns, "refund the order and email the customer about late shipping") == []


def test_stopwords_alone_never_match(fake_store, ns):
    seed(fake_store, ns)
    assert names(fake_store, ns, "to the of for") == []


def test_alias_goals_are_hidden(fake_store, ns):
    seed(fake_store, ns)
    listed = names(fake_store, ns, "enable dark mode")
    assert listed == ["enable dark mode"]
    assert any(g.text.startswith("~") for g in fake_store.list_goals(ns, SITE))


def test_skill_match_rule():
    assert (
        skill_match("I need to give a customer their money back", "set product {sku} price to {price}") == 0
    )
    assert skill_match("please refund order 55", "refund order {order_id}") == 2
    assert skill_match("refund", "refund order {order_id}") == 1
    assert skill_match("orders", "open order {order_id}") == 1  # plural folded
