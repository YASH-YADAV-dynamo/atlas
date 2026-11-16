"""Learner. Targets atlas.core.learner.report and atlas.core.locate.locate."""

from __future__ import annotations

import math

import pytest

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.types import PlanReason
from atlas.core.locate import locate
from atlas.core.planner import plan
from tests.support.builders import GOAL, SITE, click, learn, page, state_id_of

A = page("/a", "button:go")
B = page("/b", "button:back")


def spec_cost(ok: int, fail: int, ms: float) -> float:
    return -math.log((ok + 1) / (ok + fail + 2)) + ms / 10000


def only_transition(store, ns):
    transitions = store.get_map(ns, SITE).transitions
    assert len(transitions) == 1
    return transitions[0]


def test_locate_reports_whether_the_state_was_known(fake_store, ns):
    """R-LOC-1: the first sight of a page is unknown, every later one is known, same id."""
    first = locate(fake_store, ns, SITE, A)
    second = locate(
        fake_store, ns, SITE, A.model_copy(update={"title": "Another title", "text": "other data"})
    )
    assert (first.known, second.known) == (False, True)
    assert first.state_id == second.state_id


def test_counters_and_cost_follow_the_spec_formula(fake_store, ns):
    """R-LEARN-1, R-LEARN-2: ok/fail are counted, ms is the mean of successes, cost is derived."""
    for ms in (100, 200, 300):
        result = learn(fake_store, ns, A, click("go"), B, ms=ms)
    b_id = state_id_of(fake_store, ns, B)
    learn(fake_store, ns, A, click("go"), A, ok=False, expect_state=b_id, ms=5000)

    assert result.duplicate is False
    assert result.state_id == b_id
    assert [(t.ok, t.fail) for t in result.transitions] == [(3, 0)]
    edge = only_transition(fake_store, ns)
    assert (edge.ok, edge.fail) == (3, 1)
    assert edge.ms == pytest.approx(200)
    assert edge.cost == pytest.approx(spec_cost(3, 1, 200))
    assert (edge.from_state, edge.to_state) == (state_id_of(fake_store, ns, A), b_id)


def test_same_event_id_counts_once(fake_store, ns):
    """R-LEARN-3: a retried report (same event_id) does not change the counters."""
    first = learn(fake_store, ns, A, click("go"), B, event_id="retry-0001")
    second = learn(fake_store, ns, A, click("go"), B, event_id="retry-0001")
    assert (first.duplicate, second.duplicate) == (False, True)
    assert second.state_id == first.state_id
    edge = only_transition(fake_store, ns)
    assert (edge.ok, edge.fail) == (1, 0)
    assert edge.cost == pytest.approx(spec_cost(1, 0, 100))


def test_failed_exploration_step_records_nothing(fake_store, ns):
    """R-LEARN-5: a failed action with no expected destination teaches the map no edge."""
    result = learn(fake_store, ns, A, click("go"), A, ok=False)
    assert result.transitions == ()
    assert fake_store.get_map(ns, SITE).transitions == ()


def test_goal_can_be_linked_by_resending_the_event(fake_store, ns):
    """R-LEARN-4: re-sending an event with goal_reached links the goal without recounting."""
    request = PlanRequest(site=SITE, state_id=state_id_of(fake_store, ns, A), goal_text=GOAL)
    learn(fake_store, ns, A, click("go"), B, event_id="goal-00001")
    assert plan(fake_store, ns, request).reason is PlanReason.UNKNOWN_GOAL

    again = learn(fake_store, ns, A, click("go"), B, event_id="goal-00001", goal_text=GOAL, goal_reached=True)
    assert again.duplicate is True
    assert only_transition(fake_store, ns).ok == 1
    result = plan(fake_store, ns, request)
    assert result.reason is PlanReason.ROUTE_FOUND
    assert len(result.route) == 1
