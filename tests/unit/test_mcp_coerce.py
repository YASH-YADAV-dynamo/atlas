"""D13.4: lenient MCP input. The failing requests of the fresh-user run (freshuser/logs), now
accepted with a note; the contract models stay strict."""

from __future__ import annotations

import copy

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from atlas.contracts import mcp_models as m
from atlas.contracts.types import Action
from atlas.mcp.coerce import coerce
from tests.support.mcp_call import call_tool
from tests.unit.test_mcp_plan_replay import tools  # noqa: F401

SITE = "wikipedia"
KAJOL = {"url": "https://en.wikipedia.org/wiki/Kajol", "title": "Kajol - Wikipedia", "status": 200}
SEARCH = {"url": "https://en.wikipedia.org/wiki/Main_Page", "title": "Wikipedia", "elements": []}
GO = {"kind": "click", "locator": {"role": "button", "name": "Go"}}


def report(steps, **extra):
    return {
        "site": SITE,
        "goal_text": "Who is {person}?",
        "params": {"person": "Kajol"},
        "kind": "read",
        "goal_reached": True,
        "from_state": 11,
        **extra,
        "steps": steps,
    }


def test_press_with_param_inside_a_step_keeps_enter():
    """main2 line 79 (Open Library title search): press carries param and value Enter."""
    step = {
        "action": {
            "kind": "press",
            "locator": {"role": "searchbox", "name": "Search"},
            "param": "title",
            "value": "Enter",
            "fills": [{"locator": {"role": "searchbox", "name": "Search"}, "param": "title"}],
        },
        "to": KAJOL,
    }
    args, notes = coerce("atlas_report", report([step], params={"title": "Dune"}))
    action = args["steps"][0]["action"]
    assert action["value"] == "Enter" and "param" not in action
    assert action["fills"][0]["param"] == "title"
    assert notes and all(len(n) <= m.NOTE_LENGTH for n in notes)
    Action.model_validate(action)


def test_goto_locator_is_ignored():
    """main line 47: goto sent with a link locator."""
    step = {
        "action": {"kind": "goto", "locator": {"role": "link", "name": "Kajol"}, "value": KAJOL["url"]},
        "to": KAJOL,
    }
    args, notes = coerce("atlas_report", report([step]))
    assert "locator" not in args["steps"][0]["action"]
    assert "goto: locator ignored" in notes


def test_goto_url_is_taken_as_value():
    step = {"action": {"kind": "goto", "url": "/wiki/X"}, "to": KAJOL}
    args, notes = coerce("atlas_report", report([step]))
    assert args["steps"][0]["action"] == {"kind": "goto", "value": "/wiki/X"}
    assert notes


def test_in_list_inside_a_locator_is_dropped_everywhere():
    """main2 line 121 (books): in_list inside a step locator; also inside fills and a top action."""
    loc = {"in_list": True, "name": "A Light in the Attic", "role": "link"}
    action = {"kind": "click", "locator": dict(loc), "fills": [{"locator": dict(loc), "value": "x"}]}
    args, notes = coerce("atlas_report", report([{"action": action, "to": KAJOL}]))
    fixed = args["steps"][0]["action"]
    assert "in_list" not in fixed["locator"] and "in_list" not in fixed["fills"][0]["locator"]
    assert "dropped locator.in_list" in notes
    top, _ = coerce("atlas_report", {"site": SITE, "action": {"kind": "click", "locator": dict(loc)}})
    assert top["action"]["locator"] == {"name": "A Light in the Attic", "role": "link"}


def test_param_and_value_on_a_fill_keeps_the_task_param():
    fill = {"locator": {"role": "searchbox", "name": "S"}, "param": "person", "value": "Kajol"}
    step = {"action": {**GO, "fills": [fill]}, "to": KAJOL}
    args, notes = coerce("atlas_report", report([step]))
    assert args["steps"][0]["action"]["fills"][0] == {"locator": fill["locator"], "param": "person"}
    other = {"action": {**GO, "fills": [{**fill, "param": "unknown"}]}, "to": KAJOL}
    args, _ = coerce("atlas_report", report([other]))
    assert args["steps"][0]["action"]["fills"][0]["value"] == "Kajol"
    assert notes


def test_done_boolean_is_dropped():
    args, notes = coerce("atlas_report", {"site": SITE, "plan_id": "a" * 12, "done": True})
    assert "done" not in args and notes
    args, _ = coerce("atlas_report", {"site": SITE, "plan_id": "a" * 12, "done": 2})
    assert args["done"] == 2


def test_unknown_top_level_keys_are_dropped_but_plan_kind_is_kept():
    args, notes = coerce("atlas_report", {"site": SITE, "plan_id": "a" * 12, "mood": "ok"})
    assert "mood" not in args and notes == ("dropped mood",)
    plan = {"site": SITE, "goal_text": "x", "kind": "read", "snapshot_summary": SEARCH}
    args, notes = coerce("atlas_plan", plan)
    assert args["kind"] == "read" and notes == ()


def test_all_failing_requests_of_the_run_now_succeed(tools):  # noqa: F811
    """Shapes of main.jsonl lines 21, 47, 54 and main2.jsonl lines 24, 79, 121, replayed."""
    plan = call_tool(  # line 54: kind sent to atlas_plan
        tools,
        "atlas_plan",
        {
            "site": SITE,
            "goal_text": "Look up {topic} on Wikipedia",
            "params": {"topic": "Taj Mahal"},
            "kind": "read",
            "snapshot_summary": {"title": "Kajol - Wikipedia", "url": KAJOL["url"]},
        },
    )
    assert plan.needs_model
    taj = {"url": "https://en.wikipedia.org/wiki/Taj_Mahal", "title": "Taj Mahal - Wikipedia"}
    steps = [  # goto+locator, press+param+value, in_list, and no top-level ok
        {
            "action": {"kind": "goto", "locator": {"role": "link", "name": "Kajol"}, "value": KAJOL["url"]},
            "to": KAJOL,
        },
        {
            "action": {
                "kind": "press",
                "locator": {"role": "searchbox", "name": "Search"},
                "param": "topic",
                "value": "Enter",
                "fills": [{"locator": {"role": "searchbox", "name": "Search"}, "param": "topic"}],
            },
            "to": taj,
        },
        {
            "action": {"kind": "click", "locator": {"in_list": True, "role": "link", "name": "Taj Mahal"}},
            "to": {**taj, "status": 200},
        },
    ]
    request = report(
        steps,
        goal_text="Look up {topic} on Wikipedia",
        params={"topic": "Taj Mahal"},
        from_state=plan.state_id,
    )
    out = call_tool(tools, "atlas_report", request)
    assert out.learned == 3 and out.goal
    assert 0 < len(out.notes) <= m.MAX_NOTES
    assert m.response_tokens(out) <= m.MCP_RESPONSE_TOKEN_BUDGET


def test_the_contract_models_stay_strict():
    plan = {"site": "s", "goal_text": "g", "snapshot_summary": SEARCH, "mood": 1}
    with pytest.raises(ValidationError):
        m.PlanToolInput.model_validate(plan)
    with pytest.raises(ValidationError):
        Action.model_validate({"kind": "goto", "locator": {"role": "link", "name": "x"}, "value": "/"})


def test_clean_input_is_unchanged_and_not_mutated():
    clean = report([{"action": GO, "to": KAJOL}])
    frozen = copy.deepcopy(clean)
    args, notes = coerce("atlas_report", clean)
    assert args == clean and notes == () and clean == frozen


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)
shaped = st.fixed_dictionaries(
    {},
    optional={
        "action": json_values,
        "steps": json_values,
        "params": json_values,
        "done": json_values,
        "site": json_values,
        "extra": json_values,
    },
)


@given(arguments=st.one_of(json_values, shaped), tool=st.sampled_from(["atlas_report", "atlas_plan", "nope"]))
def test_coerce_never_raises_and_never_adds_a_top_level_key(arguments, tool):
    out, notes = coerce(tool, arguments)
    assert len(notes) <= m.MAX_NOTES and all(len(n) <= m.NOTE_LENGTH for n in notes)
    if isinstance(arguments, dict):
        assert isinstance(out, dict) and set(out) <= set(arguments)
