"""Slots inside fill values and case-insensitive slots in locator names (SHIP_PLAN 5, 4.3 C2).

A typed value that contains a parameter keeps the rest literal around `{slot}` ("{person} awards"),
so a search for a new subject replays. Targets atlas.core.params and the SDK's bind_action."""

from __future__ import annotations

import pytest

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.errors import MissingParamError
from atlas.contracts.types import Action, ActionKind, Fill, Locator, PlanReason, Snapshot
from atlas.core.params import bind_action, required_slots, templatise_action
from atlas.core.planner import plan
from tests.support.builders import learn, state_id_of

BOX = Locator(role="combobox", name="Search")
P = {"person": "Hrithik Roshan"}


def search(value: str) -> Action:
    return Action(kind=ActionKind.PRESS, locator=BOX, value="Enter", fills=(Fill(locator=BOX, value=value),))


def test_value_containing_a_param_gets_a_slot():
    action = templatise_action(search("Hrithik Roshan awards"), P)
    assert action.fills[0].value == "{person} awards" and action.fills[0].param is None
    assert required_slots(action) == {"person"}
    bound = bind_action(action, {"person": "Shah Rukh Khan"})
    assert bound.fills[0].value == "Shah Rukh Khan awards"


def test_value_match_ignores_case():
    action = templatise_action(search("hrithik roshan awards"), P)
    assert action.fills[0].value == "{person} awards"
    whole = templatise_action(search("HRITHIK ROSHAN"), P)
    assert (whole.fills[0].param, whole.fills[0].value) == ("person", None)


def test_exact_value_still_becomes_param_and_unrelated_values_stay():
    assert templatise_action(search("Hrithik Roshan"), P).fills[0].param == "person"
    plain = templatise_action(search("best films 2000"), P)
    assert plain.fills[0].value == "best films 2000" and not required_slots(plain)


def test_whole_words_only():
    action = templatise_action(search("Roshanara awards"), {"person": "Roshan"})
    assert action.fills[0].value == "Roshanara awards"


def test_key_and_goto_values_are_left_alone():
    press = templatise_action(search("Enter"), {"key": "Enter"})
    assert press.value is None and press.param == "key"  # unchanged rule for an exact value
    goto = Action(kind=ActionKind.GOTO, value="/wiki/Kajol")
    assert templatise_action(goto, {"topic": "Kajol"}) == goto


def test_missing_value_slot_raises():
    action = templatise_action(search("Hrithik Roshan awards"), P)
    with pytest.raises(MissingParamError):
        bind_action(action, {})


def test_locator_names_are_slotted_ignoring_case():
    pick = Action(kind=ActionKind.CLICK, locator=Locator(role="link", name="HRITHIK ROSHAN - Wikipedia"))
    action = templatise_action(pick, P)
    assert action.locator is not None and action.locator.name == "{person} - Wikipedia"
    bound = bind_action(action, {"person": "Kajol"}).locator
    assert bound is not None and bound.name == "Kajol - Wikipedia"


def test_planner_replays_a_value_slot_for_a_new_subject(fake_store, ns):
    home = Snapshot(url="https://www.bing.com/", title="Bing")
    results = Snapshot(url="https://www.bing.com/search?q=x", title="results")
    learn(
        fake_store,
        ns,
        home,
        search("Hrithik Roshan awards"),
        results,
        site="www.bing.com",
        params=P,
        goal_text="awards of Hrithik Roshan",
        goal_reached=True,
    )
    request = PlanRequest(
        site="www.bing.com",
        state_id=state_id_of(fake_store, ns, home, site="www.bing.com"),
        goal_text="awards of {person}",
        params={"person": "Kajol"},
    )
    result = plan(fake_store, ns, request)
    assert result.reason is PlanReason.ROUTE_FOUND
    assert bind_action(result.route[0].action, {"person": "Kajol"}).fills[0].value == "Kajol awards"


def test_sdk_binds_slots_inside_values():
    from atlas_sdk.client import bind_action as sdk_bind

    action = templatise_action(search("Hrithik Roshan awards"), P).model_dump(mode="json")
    bound = sdk_bind(action, {"person": "Kajol"})
    assert bound["fills"][0]["value"] == "Kajol awards"
    assert sdk_bind({"kind": "goto", "value": "/{x}"}, {}) == {"kind": "goto", "value": "/{x}"}
