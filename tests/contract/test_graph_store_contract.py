"""GraphStore contract: the in-memory fake and the HydraDB adapter must behave identically.

Every test runs twice through the `any_store` fixture ("fake", and "hydradb" when
HYDRADB_URL is set). Targets the GraphStore port in atlas/contracts/ports.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import Action, Goal, Observation, Snapshot, State
from atlas.core.cost import edge_cost
from atlas.core.ids import goal_id, transition_id
from atlas.core.signature import state_from_snapshot
from tests.support.builders import SITE, click, new_event_id, page

AT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def put(store: GraphStore, ns: str, snapshot: Snapshot) -> State:
    state = state_from_snapshot(ns, SITE, snapshot)
    store.upsert_state(ns, state)
    return state


def observe(
    store: GraphStore,
    ns: str,
    source: State,
    action: Action,
    target: State,
    *,
    ok: bool = True,
    ms: int = 100,
    event_id: str | None = None,
):
    obs = Observation(
        event_id=event_id or new_event_id(),
        transition_id=transition_id(ns, source.id, action, target.id),
        site=SITE,
        from_state=source.id,
        to_state=target.id,
        action=action,
        ok=ok,
        ms=ms,
        at=AT,
    )
    return store.record_observation(ns, obs)


def test_upsert_state_is_idempotent_and_reports_known(any_store, ns):
    """R-STORE-1: a state is stored once; the second upsert reports it as known."""
    state = state_from_snapshot(ns, SITE, page("/a", "button:go"))
    assert any_store.get_state(ns, state.id) is None
    assert any_store.upsert_state(ns, state) is False
    assert any_store.upsert_state(ns, state) is True
    assert any_store.get_state(ns, state.id) == state
    assert any_store.get_map(ns, SITE).states == (state,)


def test_observations_are_counted_once_per_event_id(any_store, ns):
    """R-STORE-2 (ADR-0001): totals are sums over distinct events; a repeated event changes nothing."""
    a, b = put(any_store, ns, page("/a", "button:go")), put(any_store, ns, page("/b", "button:back"))
    go = click("go")
    observe(any_store, ns, a, go, b, ms=100, event_id="event-one")
    observe(any_store, ns, a, go, b, ok=False, ms=9000, event_id="event-two")
    edge = observe(any_store, ns, a, go, b, ms=300, event_id="event-three")
    repeat = observe(any_store, ns, a, go, b, ms=300, event_id="event-three")

    assert (edge.ok, edge.fail) == (2, 1)
    assert edge.ms == pytest.approx(200)
    assert edge.cost == pytest.approx(edge_cost(2, 1, 200))
    assert edge.last_ok_at == AT
    assert (edge.from_state, edge.to_state, edge.action) == (a.id, b.id, go)
    assert repeat == edge
    assert any_store.has_event(ns, "event-three") is True
    assert any_store.has_event(ns, "event-never") is False
    assert any_store.get_map(ns, SITE).transitions == (edge,)


def test_parallel_actions_between_two_states_stay_separate(any_store, ns):
    """R-STORE-3 (ADR-0003): two different actions A->B are two transitions with their own stats."""
    a, b = (
        put(any_store, ns, page("/a", "link:menu", "link:shortcut")),
        put(any_store, ns, page("/b", "button:x")),
    )
    menu = observe(any_store, ns, a, click("menu", "link"), b, ms=100)
    shortcut = observe(any_store, ns, a, click("shortcut", "link"), b, ms=4000)
    observe(any_store, ns, a, click("shortcut", "link"), b, ok=False)
    by_id = {t.id: t for t in any_store.get_map(ns, SITE).transitions}
    assert menu.id != shortcut.id
    assert (by_id[menu.id].ok, by_id[menu.id].fail) == (1, 0)
    assert (by_id[shortcut.id].ok, by_id[shortcut.id].fail) == (1, 1)


def test_shortest_paths_are_cheapest_first_and_bounded(any_store, ns):
    """R-STORE-4: paths end with the target transition, come cheapest first, and respect max_steps."""
    a = put(any_store, ns, page("/a", "button:slow", "button:fast"))
    b = put(any_store, ns, page("/b", "button:on"))
    c = put(any_store, ns, page("/c", "button:finish"))
    d = put(any_store, ns, page("/d", "button:done"))
    observe(any_store, ns, a, click("slow"), c, ms=30_000)
    observe(any_store, ns, a, click("fast"), b)
    observe(any_store, ns, b, click("on"), c)
    target = observe(any_store, ns, c, click("finish"), d)

    paths = any_store.shortest_paths(ns, a.id, target.id, max_steps=6, path_count=5)
    assert [[s.action.locator.name for s in p.steps] for p in paths] == [
        ["fast", "on", "finish"],
        ["slow", "finish"],
    ]
    assert paths[0].cost < paths[1].cost
    for path in paths:
        assert path.cost == pytest.approx(sum(step.cost for step in path.steps))
        assert path.steps[0].from_state == a.id
        assert path.steps[-1].id == target.id
        assert all(
            prev.to_state == nxt.from_state for prev, nxt in zip(path.steps, path.steps[1:], strict=False)
        )

    only_short = any_store.shortest_paths(ns, a.id, target.id, max_steps=2, path_count=5)
    assert [[s.action.locator.name for s in p.steps] for p in only_short] == [["slow", "finish"]]
    assert any_store.shortest_paths(ns, a.id, target.id, max_steps=1, path_count=5) == ()
    assert any_store.shortest_paths(ns, c.id, target.id, max_steps=1, path_count=5)[0].steps == (target,)
    assert any_store.shortest_paths(ns, d.id, target.id, max_steps=6, path_count=5) == ()


def test_goal_links_are_idempotent(any_store, ns):
    """R-STORE-6: linking a goal twice keeps one link; an unknown goal has no targets."""
    a, b = put(any_store, ns, page("/a", "button:go")), put(any_store, ns, page("/b", "button:back"))
    edge = observe(any_store, ns, a, click("go"), b)
    goal = Goal(id=goal_id(ns, SITE, "do the thing"), site=SITE, text="do the thing")
    any_store.link_goal(ns, goal, edge.id)
    any_store.link_goal(ns, goal, edge.id)
    assert any_store.goal_targets(ns, goal.id) == (edge.id,)
    assert any_store.goal_targets(ns, goal_id(ns, SITE, "something else")) == ()


def test_namespaces_are_isolated(any_store, ns):
    """R-TEN-1: one namespace cannot read another's states, events, goals, paths or map."""
    other = f"{ns}x"
    a, b = put(any_store, ns, page("/a", "button:go")), put(any_store, ns, page("/b", "button:back"))
    edge = observe(any_store, ns, a, click("go"), b, event_id="private-event")
    goal = Goal(id=goal_id(ns, SITE, "do the thing"), site=SITE, text="do the thing")
    any_store.link_goal(ns, goal, edge.id)

    assert any_store.get_state(other, a.id) is None
    assert any_store.has_event(other, "private-event") is False
    assert any_store.goal_targets(other, goal.id) == ()
    assert any_store.shortest_paths(other, a.id, edge.id, max_steps=4, path_count=3) == ()
    empty = any_store.get_map(other, SITE)
    assert (empty.states, empty.transitions) == ((), ())
