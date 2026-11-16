"""ContextStore contract (goals as skills, transitions by id, entities), on the fake and on
HydraDB. Targets the ContextStore port; lane M adds these methods to both stores."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas.contracts.types import Entity, Goal
from atlas.core.ids import goal_id, stable_id
from tests.contract.test_graph_store_contract import observe, put
from tests.support.builders import SITE, click, page

AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(any_store):
    if not hasattr(any_store, "list_goals"):
        pytest.skip("MCP wave: ContextStore methods are added by lane M")
    return any_store


def learned_edge(store, ns):
    a, b = put(store, ns, page("/a", "button:go")), put(store, ns, page("/b", "button:back"))
    return observe(store, ns, a, click("go"), b)


def test_goals_are_listed_per_site_and_namespace(store, ns):
    """R-CTX-1: list_goals returns the site's goals ordered by id and nothing from elsewhere."""
    edge = learned_edge(store, ns)
    goals = sorted(
        (
            Goal(id=goal_id(ns, SITE, text), site=SITE, text=text)
            for text in ("refund order {order_id}", "open settings")
        ),
        key=lambda goal: goal.id,
    )
    for goal in goals:
        store.link_goal(ns, goal, edge.id)
        store.link_goal(ns, goal, edge.id)
    assert store.list_goals(ns, SITE) == tuple(goals)
    assert store.list_goals(ns, "other.test") == ()
    assert store.list_goals(f"{ns}x", SITE) == ()


def test_transition_lookup_by_id(store, ns):
    """R-CTX-2: get_transition returns the stored transition with current totals, or None."""
    edge = learned_edge(store, ns)
    assert store.get_transition(ns, edge.id) == edge
    assert store.get_transition(ns, edge.id + 1) is None
    assert store.get_transition(f"{ns}x", edge.id) is None


def test_entity_upsert_keeps_the_latest_sighting(store, ns):
    """R-CTX-3: record_entity is idempotent and the newest sighting wins; namespaces are isolated."""
    first_edge = learned_edge(store, ns)
    c = put(store, ns, page("/c", "button:more"))
    b = put(store, ns, page("/b", "button:back"))
    second_edge = observe(store, ns, b, click("back"), c)
    entity_id = stable_id(ns, "entity", SITE, "order_id", "1042")
    first = Entity(
        id=entity_id, site=SITE, type="order_id", key="1042", transition_id=first_edge.id, seen_at=AT
    )
    later = first.model_copy(update={"transition_id": second_edge.id, "seen_at": AT + timedelta(minutes=5)})

    assert store.get_entity(ns, entity_id) is None
    store.record_entity(ns, first)
    store.record_entity(ns, first)
    assert store.get_entity(ns, entity_id) == first
    store.record_entity(ns, later)
    assert store.get_entity(ns, entity_id) == later
    assert store.get_entity(f"{ns}x", entity_id) is None
