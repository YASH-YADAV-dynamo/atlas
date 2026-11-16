"""Goal matching tiers (SHIP_PLAN 5, D4) and suite D (section 8: no automatic false match).

Exact key always matches; same words match automatically for read goals only; similar goals are
only offered (`match: "similar"` with the stored `goal`, no route). Aliases come from reported
successes only, and `~` alias goals stay out of listings. Targets atlas.core.goals and the planner.
"""

from __future__ import annotations

import pytest

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.types import GoalKind, Params, PlanReason, PlanResult, ReportEvent
from atlas.core.goals import content_words, same_words_key, visible_goals
from atlas.core.learner import report
from atlas.core.planner import plan
from tests.support.builders import SITE, FixedClock, click, new_event_id, page, state_id_of

HOME = page("/", "link:Go", "link:Help")
DONE = page("/done", "link:Back")


def teach(
    store,
    ns,
    goal_text: str,
    params: Params | None = None,
    *,
    kind: GoalKind = "act",
    ok: bool = True,
    control: str = "Go",
):
    from atlas.core.locate import locate

    start = locate(store, ns, SITE, HOME).state_id
    event = ReportEvent(
        site=SITE,
        from_state=start,
        action=click(control, "link"),
        to_snapshot=DONE,
        ok=ok,
        ms=50,
        event_id=new_event_id(),
        goal_text=goal_text,
        params=params or {},
        goal_reached=True,
        kind=kind,
    )
    return report(store, FixedClock(), ns, event)


def ask(store, ns, goal_text: str, params: Params | None = None) -> PlanResult:
    request = PlanRequest(
        site=SITE, state_id=state_id_of(store, ns, HOME), goal_text=goal_text, params=params or {}
    )
    return plan(store, ns, request)


# ---- normalisation ---------------------------------------------------------------------


def test_same_words_key_drops_stopwords_retrieval_verbs_and_plural_s():
    a = same_words_key("Find the awards won by {person}")
    assert a == same_words_key("What awards has {person} won?")
    assert a == "award won {person}"


def test_action_verbs_negations_and_numbers_are_kept():
    assert content_words("enable dark mode") != content_words("disable dark mode")
    assert "not" in content_words("do not notify me")
    assert "1005" in content_words("refund order 1005")
    assert "off" in content_words("turn off alerts")


# ---- tiers ------------------------------------------------------------------------------


def test_exact_wording_matches_with_match_exact(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "find the awards won by Hrithik Roshan", {"person": "Hrithik Roshan"})
    assert result.reason is PlanReason.ROUTE_FOUND
    assert (result.match, result.goal) == ("exact", None)


def test_same_words_match_is_automatic_for_read_goals(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "What awards has Hrithik Roshan won?", {"person": "Hrithik Roshan"})
    assert result.reason is PlanReason.ROUTE_FOUND and result.route
    assert result.match == "same_words"
    assert result.goal == "find the awards won by {person}"


def test_same_words_match_of_an_act_goal_is_only_offered(fake_store, ns):
    teach(fake_store, ns, "Move order 7 to archive", {"order_id": "7"})
    result = ask(fake_store, ns, "archive: move order 9", {"order_id": "9"})
    assert (result.needs_model, result.reason, result.route) == (True, PlanReason.UNKNOWN_GOAL, ())
    assert result.match == "similar"
    assert result.goal == "move order {order_id} to archive"


def test_similar_wording_is_offered_never_used(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "Hrithik Roshan awards list", {"person": "Hrithik Roshan"})
    assert (result.needs_model, result.reason) == (True, PlanReason.UNKNOWN_GOAL)
    assert (result.match, result.goal) == ("similar", "find the awards won by {person}")
    # The agent accepts by planning with the stored goal.
    accepted = ask(fake_store, ns, result.goal, {"person": "Hrithik Roshan"})
    assert accepted.reason is PlanReason.ROUTE_FOUND and accepted.match == "exact"


def test_similar_needs_the_same_slots_and_a_clear_winner(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    other_slot = ask(fake_store, ns, "{film} awards list", {"film": "Lagaan"})
    assert other_slot.match is None and other_slot.goal is None
    no_slot = ask(fake_store, ns, "awards list")
    assert no_slot.match is None


def test_two_equally_similar_goals_offer_nothing(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    teach(fake_store, ns, "Find the awards given to Kajol", {"person": "Kajol"}, kind="read", control="Help")
    tied = ask(fake_store, ns, "{person} awards", {"person": "Hrithik Roshan"})
    assert (tied.match, tied.goal) == (None, None)


def test_a_confirmed_similar_wording_becomes_an_exact_alias(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    first = ask(fake_store, ns, "Hrithik Roshan awards list", {"person": "Hrithik Roshan"})
    assert first.match == "similar"
    # The agent ran the offered route and reported success under its own wording.
    teach(fake_store, ns, "Hrithik Roshan awards list", {"person": "Hrithik Roshan"}, kind="read")
    again = ask(fake_store, ns, "Deepika Padukone awards list", {"person": "Deepika Padukone"})
    assert again.reason is PlanReason.ROUTE_FOUND and again.match == "exact"


def test_aliases_are_learned_only_from_reported_successes(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read", ok=False)
    assert fake_store.list_goals(ns, SITE) == ()
    result = ask(fake_store, ns, "What awards has Kajol won?", {"person": "Kajol"})
    assert result.match is None and result.reason is PlanReason.UNKNOWN_GOAL


def test_alias_goals_are_stored_but_hidden_from_listings(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    stored = {g.text for g in fake_store.list_goals(ns, SITE)}
    assert stored == {"find the awards won by {person}", "~r award won {person}"}
    assert {g.text for g in visible_goals(fake_store.list_goals(ns, SITE))} == {
        "find the awards won by {person}"
    }


def test_unknown_goal_omits_match(fake_store, ns):
    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "book a flight to {city}", {"city": "Pune"})
    assert (result.match, result.goal, result.params) == (None, None, None)


def test_rest_plan_carries_match_and_goal(fake_store, ns):
    from atlas.contracts.api_models import PlanResponse

    teach(fake_store, ns, "Find the awards won by Kajol", {"person": "Kajol"}, kind="read")
    result = ask(fake_store, ns, "What awards has Kajol won?", {"person": "Kajol"})
    body = PlanResponse.model_validate(result.model_dump()).model_dump(exclude_none=True)
    assert body["match"] == "same_words" and body["goal"] == "find the awards won by {person}"


# ---- suite D: pairs that must never match automatically ----------------------------------

# (stored goal, stored params, kind, asked goal, asked params)
SUITE_D: list[tuple[str, Params, GoalKind, str, Params]] = [
    ("refund order 1005", {"order_id": "1005"}, "act", "cancel order 1005", {"order_id": "1005"}),
    ("enable {setting}", {"setting": "x"}, "act", "disable {setting}", {"setting": "x"}),
    (
        "move {sku} from cart to wishlist",
        {"sku": "s"},
        "act",
        "move {sku} from wishlist to cart",
        {"sku": "s"},
    ),
    ("open order 42", {"order_id": "42"}, "read", "open orders", {}),
    ("open order", {}, "read", "open orders", {}),
    ("add {sku} to cart", {"sku": "s"}, "act", "remove {sku} from cart", {"sku": "s"}),
    ("subscribe to the newsletter", {}, "act", "unsubscribe from the newsletter", {}),
    ("show paid invoices", {}, "read", "show unpaid invoices", {}),
    ("delete user {user_id}", {"user_id": "u"}, "act", "create user {user_id}", {"user_id": "u"}),
    ("turn on dark mode", {}, "act", "turn off dark mode", {}),
    ("show orders from 2023", {}, "read", "show orders from 2024", {}),
    ("find flights to {city}", {"city": "Pune"}, "read", "find hotels in {city}", {"city": "Pune"}),
    ("do not notify me about orders", {}, "act", "notify me about orders", {}),
]


@pytest.mark.parametrize(("stored", "stored_params", "kind", "asked", "asked_params"), SUITE_D)
def test_suite_d_no_automatic_false_match(fake_store, ns, stored, stored_params, kind, asked, asked_params):
    teach(fake_store, ns, stored, stored_params, kind=kind)
    forward = ask(fake_store, ns, asked, asked_params)
    assert forward.needs_model and not forward.route
    assert forward.match != "exact" and forward.match != "same_words"


@pytest.mark.parametrize(("stored", "stored_params", "kind", "asked", "asked_params"), SUITE_D)
def test_suite_d_no_automatic_false_match_reversed(
    fake_store, ns, stored, stored_params, kind, asked, asked_params
):
    teach(fake_store, ns, asked, asked_params, kind=kind)
    backward = ask(fake_store, ns, stored, stored_params)
    assert backward.needs_model and not backward.route
    assert backward.match != "exact" and backward.match != "same_words"


def test_suite_d_also_holds_for_read_kind(fake_store, ns):
    """Even taught as a read goal, word-order and one-word pairs are never served automatically."""
    for stored, stored_params, _, _, _ in SUITE_D:
        if stored == "move {sku} from cart to wishlist":
            continue  # same words in another order: automatic for read goals by design (SHIP_PLAN 5)
        teach(fake_store, ns, stored, stored_params, kind="read")
    for _, _, _, asked, asked_params in SUITE_D:
        if asked == "move {sku} from wishlist to cart":
            continue
        result = ask(fake_store, ns, asked, asked_params)
        assert not result.route, asked
