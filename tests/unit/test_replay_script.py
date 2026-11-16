"""Replay script generator (SHIP_PLAN 3.1): escaping, the 900-character cap, the token budget."""

from __future__ import annotations

import json

from atlas.contracts import mcp_models as m
from atlas.contracts.types import Action, ActionKind, Fill, Locator, PlanReason, RouteStep
from atlas.core.replay import MAX_SCRIPT_CHARS, build_script


def search() -> RouteStep:
    box = Locator(role="searchbox", name="Search")
    go = Action(
        kind=ActionKind.CLICK,
        locator=Locator(role="button", name="Go"),
        fills=(Fill(locator=box, param="q"),),
    )
    return RouteStep(action=go, expect_state=2, reliability=0.9)


def test_slots_are_bound_and_strings_are_json_literals():
    value = 'Shah "Rukh" \\ Khan\n'
    script = build_script([search()], {"q": value})
    assert script is not None and script.steps == 1
    assert script.text.startswith("async (page) =>")
    assert json.dumps(value) in script.text
    assert "done: n" in script.text


def test_hostile_values_stay_inside_a_string_literal():
    evil = '"); await page.evaluate("steal()"); ("'
    script = build_script([search()], {"q": evil})
    assert script is not None
    assert json.dumps(evil) in script.text
    assert script.text.count("page.evaluate") == 1  # only inside the quoted literal


def test_line_separators_are_escaped():
    seps = chr(0x2028) + chr(0x2029)
    script = build_script([search()], {"q": "a" + seps + "b"})
    assert script is not None
    assert chr(0x2028) not in script.text and chr(0x2029) not in script.text
    assert chr(92) + "u2028" in script.text


def test_unbound_slot_gives_no_script():
    assert build_script([search()], {}) is None


def test_the_script_is_whole_or_absent_and_capped():
    many = [search() for _ in range(40)]
    script = build_script(many, {"q": "x"})
    assert script is not None
    assert len(script.text) <= MAX_SCRIPT_CHARS
    assert 1 <= script.steps < 40
    assert script.text.endswith("}")
    assert f"n = {script.steps};" in script.text and f"n = {script.steps + 1};" not in script.text
    assert build_script(many, {"q": "y" * 1000}) is None  # not even one step fits


def test_the_caller_size_check_limits_steps():
    two = build_script([search(), search()], {"q": "x"})
    assert two is not None and two.steps == 2
    one = build_script([search(), search()], {"q": "x"}, fits=lambda text: len(text) < len(two.text))
    assert one is not None and one.steps == 1


def test_a_full_script_fits_the_response_token_budget():
    script = build_script([search() for _ in range(40)], {"q": "x" * 100})
    assert script is not None
    out = m.PlanToolOutput(
        state_id=2**53 - 1,
        plan_id="a" * 12,
        script=script.text,
        script_steps=script.steps,
        needs_model=False,
        reason=PlanReason.ROUTE_FOUND,
    )
    assert m.response_tokens(out) <= m.MCP_RESPONSE_TOKEN_BUDGET
