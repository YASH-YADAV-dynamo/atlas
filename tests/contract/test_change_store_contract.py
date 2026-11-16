"""ChangeStore contract (wave 3), on the fake and on HydraDB: outcome history and change events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas.contracts.types import ChangeEvent, Observation
from atlas.core.ids import stable_id, transition_id
from tests.contract.test_graph_store_contract import put
from tests.support.builders import SITE, click, new_event_id, page

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(any_store):
    if not hasattr(any_store, "recent_outcomes"):
        pytest.skip("wave 3: ChangeStore methods are added by lane R")
    return any_store


def edge(store, ns, outcomes: list[bool], path: str = "/a"):
    """Record `outcomes` one second apart on one transition; returns the final Transition."""
    a, b = put(store, ns, page(path, "button:go")), put(store, ns, page("/b", "button:back"))
    result = None
    for index, ok in enumerate(outcomes):
        obs = Observation(
            event_id=new_event_id(),
            transition_id=transition_id(ns, a.id, click("go"), b.id),
            site=SITE,
            from_state=a.id,
            to_state=b.id,
            action=click("go"),
            ok=ok,
            ms=100,
            at=T0 + timedelta(seconds=index),
        )
        result = store.record_observation(ns, obs)
    return result


def change_for(ns, transition, at: datetime, **extra) -> ChangeEvent:
    return ChangeEvent(
        id=stable_id(ns, "change", transition.id),
        site=SITE,
        transition_id=transition.id,
        from_state=transition.from_state,
        expected_state=transition.to_state,
        action=transition.action,
        first_seen=at,
        **extra,
    )


def test_recent_outcomes_are_newest_first(store, ns):
    """R-CHG-3: outcome history is ordered by event time, newest first, and limited."""
    transition = edge(store, ns, [True, True, False, True, False, False])
    assert store.recent_outcomes(ns, transition.id, 2) == (False, False)
    assert store.recent_outcomes(ns, transition.id, 4) == (False, False, True, False)
    assert store.recent_outcomes(ns, transition.id, 50) == (False, False, True, False, True, True)
    assert store.recent_outcomes(f"{ns}x", transition.id, 5) == ()
    assert store.recent_outcomes(ns, transition.id + 1, 5) == ()


def test_change_upsert_keeps_first_seen_and_gains_the_replacement(store, ns):
    """R-CHG-3: a change is stored once; later records add the replacement, never move first_seen."""
    old = edge(store, ns, [True] * 5 + [False, False])
    new = edge(store, ns, [True], path="/elsewhere")
    first = change_for(ns, old, T0 + timedelta(minutes=1))
    store.record_change(ns, first)
    store.record_change(ns, first)
    assert store.list_changes(ns, SITE) == (first,)

    later = change_for(ns, old, T0 + timedelta(minutes=9), actual_state=new.to_state, replacement_id=new.id)
    store.record_change(ns, later)
    merged = first.model_copy(update={"actual_state": new.to_state, "replacement_id": new.id})
    assert store.list_changes(ns, SITE) == (merged,)
    store.record_change(ns, change_for(ns, old, T0 + timedelta(minutes=20)))
    assert store.list_changes(ns, SITE) == (merged,)


def test_changes_are_listed_newest_first_per_site_and_namespace(store, ns):
    """R-CHG-3, R-TEN-1: list_changes is newest first and isolated by site and namespace."""
    one = edge(store, ns, [True] * 5 + [False, False], path="/one")
    two = edge(store, ns, [True] * 5 + [False, False], path="/two")
    early = change_for(ns, one, T0 + timedelta(minutes=1))
    late = change_for(ns, two, T0 + timedelta(minutes=2))
    store.record_change(ns, early)
    store.record_change(ns, late)
    assert store.list_changes(ns, SITE) == (late, early)
    assert store.list_changes(ns, "other.test") == ()
    assert store.list_changes(f"{ns}x", SITE) == ()
