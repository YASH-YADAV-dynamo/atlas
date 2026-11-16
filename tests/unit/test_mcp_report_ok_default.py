"""D13.1: `ok` is optional on atlas_report and /v1/finish; an explicit `ok` always wins."""

from __future__ import annotations

import pytest

from atlas.contracts import mcp_models as m
from atlas.contracts.api_models import FinishRequest, FinishStep
from atlas.contracts.types import Action, ActionKind, Locator
from atlas.core.session import resolve_ok
from tests.support.mcp_call import call_tool
from tests.unit.test_mcp_plan_replay import GOAL, HOME, SITE, article, ask, click, results, teach, tools  # noqa: F401


def batch_args(state_id: int, **extra) -> dict:
    """The first failing request of the fresh-user run: a batch report with no top-level ok."""
    return {
        "site": SITE,
        "goal_text": GOAL,
        "params": {"topic": "Kajol"},
        "kind": "read",
        "goal_reached": True,
        "from_state": state_id,
        "steps": [
            {
                "action": {"kind": "click", "locator": {"role": "button", "name": "Go"}},
                "to": results("Kajol").model_dump(exclude_none=True),
                "ok": True,
            },
            {
                "action": {"kind": "click", "locator": {"role": "link", "name": "Kajol"}},
                "to": article("Kajol").model_dump(exclude_none=True),
            },
        ],
        **extra,
    }


def test_the_exact_t0_request_is_accepted_and_learns(tools):  # noqa: F811
    p = ask(tools, "Kajol")
    out = call_tool(tools, "atlas_report", batch_args(p.state_id))
    assert out.learned == 2 and out.goal
    again = ask(tools, "Kajol")
    assert not again.needs_model and again.match == "exact"


def test_one_failed_step_gives_ok_false_and_no_goal(tools):  # noqa: F811
    p = ask(tools, "Kajol")
    args = batch_args(p.state_id)
    args["steps"][1]["ok"] = False
    out = call_tool(tools, "atlas_report", args)
    assert out.goal is None
    assert ask(tools, "Kajol").needs_model


def test_explicit_ok_wins_over_the_default(tools):  # noqa: F811
    p = ask(tools, "Kajol")
    out = call_tool(tools, "atlas_report", batch_args(p.state_id, ok=False))
    assert out.goal is None  # steps were all ok, but the caller said the task failed
    assert resolve_ok(finish_request(ok=False), None) is False
    assert resolve_ok(finish_request(ok=True, step_ok=False), None) is True


def finish_request(ok: bool | None = None, step_ok: bool = True, done: int | None = None, plan: bool = False):
    step = FinishStep(
        action=Action(kind=ActionKind.CLICK, locator=Locator(role="button", name="Go")), to=HOME, ok=step_ok
    )
    return FinishRequest(
        site=SITE, from_state=1, steps=(step,), ok=ok, done=done, plan_id="a" * 12 if plan else None
    )


def test_batch_default_follows_the_steps():
    assert resolve_ok(finish_request(), None) is True
    assert resolve_ok(finish_request(step_ok=False), None) is False


def test_replay_default_follows_done_against_the_route(tools):  # noqa: F811
    teach(tools)
    p = ask(tools, "Hrithik Roshan")
    assert p.plan_id and len(p.route) == 2
    # Fewer steps than the route: a failed replay, so the next step is charged one failure.
    call_tool(
        tools, "atlas_report", {"site": SITE, "plan_id": p.plan_id, "done": 1, "from_state": p.state_id}
    )
    ok_counts = sorted((t.ok, t.fail) for t in tools.store.get_map(tools.namespace, SITE).transitions)
    assert any(fail for _, fail in ok_counts)


def test_replay_without_done_or_with_full_done_is_ok(tools):  # noqa: F811
    teach(tools)
    p = ask(tools, "Hrithik Roshan")
    out = call_tool(tools, "atlas_report", {"site": SITE, "plan_id": p.plan_id, "done": len(p.route)})
    assert out.learned == len(p.route)
    q = ask(tools, "Kajol")
    out = call_tool(tools, "atlas_report", {"site": SITE, "plan_id": q.plan_id})
    assert out.learned == len(q.route)


def test_single_step_defaults_to_ok(tools):  # noqa: F811
    p = ask(tools, "A")
    out = call_tool(
        tools,
        "atlas_report",
        {
            "site": SITE,
            "from_state": p.state_id,
            "action": {"kind": "click", "locator": {"role": "button", "name": "Go"}},
            "to_snapshot_summary": results("A").model_dump(exclude_none=True),
        },
    )
    assert out.learned is None and not out.duplicate


@pytest.mark.parametrize("ok", [None, True, False])
def test_the_contract_accepts_each_value(ok):
    assert m.ReportInput(site=SITE, plan_id="a" * 12, ok=ok).ok is ok
