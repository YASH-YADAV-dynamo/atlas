"""atlas_plan as the single entry call: plan_id, open_url, script, route (SHIP_PLAN 3.1)."""

from __future__ import annotations

import pytest

from atlas.contracts import mcp_models as m
from atlas.contracts.errors import AtlasError
from atlas.contracts.types import Action, ActionKind, Element, Locator, Snapshot
from atlas.mcp.tools import AtlasTools
from tests.support.builders import FixedClock

SITE = "wiki.test"
GOAL = "Open the article about {topic}"


def snap(path: str, *controls: str, title: str = "") -> Snapshot:
    els = tuple(Element(role=c.split(":", 1)[0], name=c.split(":", 1)[1]) for c in controls)
    return Snapshot(url=f"https://{SITE}{path}", title=title or path, elements=els)


HOME = snap("/", "searchbox:Search", "button:Go")


def results(t: str) -> Snapshot:
    return snap(f"/w/index.php?search={t.replace(' ', '+')}", f"link:{t}", "button:Go", title="Results")


def article(t: str) -> Snapshot:
    return snap(f"/wiki/{t.replace(' ', '_')}", "link:Edit", title=f"{t} - Wiki")


@pytest.fixture
def tools(fake_store, ns):
    return AtlasTools(fake_store, FixedClock(), ns, SITE)


def click(name: str, role: str = "button") -> Action:
    return Action(kind=ActionKind.CLICK, locator=Locator(role=role, name=name))


def ask(tools, topic, **extra):
    return tools.plan(
        m.PlanToolInput(site=SITE, snapshot_summary=HOME, goal_text=GOAL, params={"topic": topic}, **extra)
    )


def teach(tools, topic="Shah Rukh Khan", kind="read"):
    p = ask(tools, topic)
    fill = Action(kind=ActionKind.FILL, locator=Locator(role="searchbox", name="Search"), value=topic)
    out = tools.report(
        m.ReportInput(
            site=SITE,
            from_state=p.state_id,
            ok=True,
            steps=(
                m.ReportStep(action=fill, to=HOME),
                m.ReportStep(action=click("Go"), to=results(topic)),
                m.ReportStep(action=click(topic, "link"), to=article(topic)),
            ),
            goal_text=GOAL,
            params={"topic": topic},
            goal_reached=True,
            kind=kind,
        )
    )
    return p, out


def test_a_miss_returns_needs_model_and_no_plan_id(tools):
    p = ask(tools, "A")
    assert p.needs_model and p.plan_id is None and p.script is None


def test_known_task_returns_plan_id_and_open_url(tools):
    teach(tools)
    p = ask(tools, "Shah Rukh Khan")
    assert not p.needs_model and p.plan_id and len(p.plan_id) == 12
    assert p.open_url == f"https://{SITE}/wiki/Shah_Rukh_Khan"


def test_script_replaces_the_route(tools):
    teach(tools)
    p = ask(tools, "Hrithik Roshan", replay="script")
    assert p.script and p.route == () and p.script_steps == 2
    assert "Hrithik Roshan" in p.script and len(p.script) <= 900
    assert m.response_tokens(p) <= m.MCP_RESPONSE_TOKEN_BUDGET


def test_replay_outcome_credits_the_issued_plan_once(tools):
    teach(tools)
    p = ask(tools, "Hrithik Roshan")
    assert p.plan_id
    first = tools.report(m.ReportInput(site=SITE, plan_id=p.plan_id, done=2, ok=True, event_id="replay-0001"))
    assert first.learned is not None
    again = tools.report(m.ReportInput(site=SITE, plan_id=p.plan_id, done=2, ok=True, event_id="replay-0001"))
    assert again.duplicate


def test_failed_replay_is_charged_to_the_next_step(tools):
    teach(tools)
    p = ask(tools, "Hrithik Roshan")
    out = tools.report(
        m.ReportInput(site=SITE, plan_id=p.plan_id, done=1, ok=False, to_snapshot_summary=HOME)
    )
    assert out.learned == 1


def test_unknown_plan_id_alone_is_an_error(tools):
    with pytest.raises(AtlasError):
        tools.report(m.ReportInput(site=SITE, plan_id="a" * 12, ok=True))
