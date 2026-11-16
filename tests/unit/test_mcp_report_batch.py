"""atlas_report forms (SHIP_PLAN 3.2): exactly one of single, batch, replay."""
# ruff: noqa: F811, F401

from __future__ import annotations

import jsonschema
import pytest
from pydantic import ValidationError

from atlas.contracts import mcp_models as m
from atlas.contracts.types import Action, ActionKind, Locator
from atlas.mcp.catalog import listed_schema
from tests.unit.test_mcp_plan_replay import GOAL, HOME, SITE, article, ask, results, teach, tools

CLICK = Action(kind=ActionKind.CLICK, locator=Locator(role="button", name="Go"))


def step() -> m.ReportStep:
    return m.ReportStep(action=CLICK, to=HOME)


def test_exactly_one_form_is_accepted():
    with pytest.raises(ValidationError):
        m.ReportInput(site=SITE, ok=True)
    with pytest.raises(ValidationError):
        m.ReportInput(
            site=SITE, ok=True, from_state=1, action=CLICK, to_snapshot_summary=HOME, plan_id="a" * 12
        )
    with pytest.raises(ValidationError):  # single needs the page it led to
        m.ReportInput(site=SITE, ok=True, from_state=1, action=CLICK)
    with pytest.raises(ValidationError):
        m.ReportInput(site=SITE, ok=True, done=1, from_state=1, steps=(step(),))
    with pytest.raises(ValidationError):  # at most 12 steps
        m.ReportInput(site=SITE, ok=True, from_state=1, steps=tuple(step() for _ in range(13)))


def test_single_step_still_works(tools):
    p = ask(tools, "A")
    out = tools.report(
        m.ReportInput(
            site=SITE, from_state=p.state_id, action=CLICK, ok=True, to_snapshot_summary=results("A")
        )
    )
    assert out.event_id and out.learned is None


def test_batch_learns_once_then_the_goal_is_known(tools):
    _, first = teach(tools)
    assert first.learned == 2 and first.goal and not first.duplicate
    p = ask(tools, "Hrithik Roshan")
    assert not p.needs_model and p.match == "exact"


def test_batch_with_a_failed_step_does_not_link_the_goal(tools):
    p = ask(tools, "A")
    out = tools.report(
        m.ReportInput(
            site=SITE,
            from_state=p.state_id,
            ok=False,
            goal_text=GOAL,
            params={"topic": "A"},
            goal_reached=True,
            steps=(m.ReportStep(action=CLICK, to=article("A"), ok=False),),
        )
    )
    assert out.goal is None


def test_listed_schema_accepts_the_three_forms():
    spec = next(s for s in m.tools_for(m.Mode.PLANNER) if s.name == "atlas_report")
    schema = listed_schema(spec, m.Mode.PLANNER)
    page = {"url": "https://wiki.test/"}
    act = {"kind": "click", "locator": {"role": "button", "name": "Go"}}
    for args in (
        {"site": SITE, "ok": True, "plan_id": "a" * 12, "done": 2},
        {"site": SITE, "ok": True, "from_state": 1, "steps": [{"action": act, "to": page}]},
        {"site": SITE, "ok": True, "from_state": 1, "action": act, "to_snapshot_summary": page},
    ):
        jsonschema.validate(args, schema)
        m.ReportInput.model_validate(args)
