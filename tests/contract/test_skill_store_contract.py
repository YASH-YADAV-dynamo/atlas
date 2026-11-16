"""SkillStore contract (increment 7), on the fake and on HydraDB: run history, skills as one-hop
shortcuts for the path query, and the sleep-phase log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from atlas.contracts.types import Observation, RunStep, Skill, SleepLogEntry, SleepLogKind
from atlas.core.ids import stable_id, transition_id
from tests.contract.test_graph_store_contract import put
from tests.support.builders import SITE, click, new_event_id, page

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
NAMES = "abcdefghijkl"


@pytest.fixture
def store(any_store):
    if not hasattr(any_store, "upsert_skill"):
        pytest.skip("increment 7: SkillStore methods are added by lane K")
    return any_store


def chain(store, ns, length: int, run_id: str | None = "run-0001"):
    """States s0..s(length) joined by `length` transitions, each observed once in order."""
    states = [put(store, ns, page(f"/k/{NAMES[i]}", f"button:go-{NAMES[i]}")) for i in range(length + 1)]
    transitions = []
    for i in range(length):
        action = click(f"go-{NAMES[i]}")
        obs = Observation(
            event_id=new_event_id(),
            transition_id=transition_id(ns, states[i].id, action, states[i + 1].id),
            site=SITE,
            from_state=states[i].id,
            to_state=states[i + 1].id,
            action=action,
            ok=True,
            ms=100,
            at=T0 + timedelta(seconds=i),
            run_id=run_id,
        )
        transitions.append(store.record_observation(ns, obs))
    return states, transitions


def skill_over(ns, transitions, cost: float) -> Skill:
    via = tuple(t.id for t in transitions)
    return Skill(
        id=stable_id(ns, "skill", *via),
        site=SITE,
        from_state=transitions[0].from_state,
        to_state=transitions[-1].to_state,
        via=via,
        runs=3,
        cost=cost,
    )


def test_run_steps_come_back_in_run_order(store, ns):
    """R-SKILL-6: steps recorded with a run id are returned ordered by run and time; steps
    without one are not; namespaces and sites are isolated."""
    _, transitions = chain(store, ns, 4)
    chain(store, ns, 2, run_id=None)
    expected = tuple(
        RunStep(run_id="run-0001", transition_id=t.id, ok=True, at=T0 + timedelta(seconds=i))
        for i, t in enumerate(transitions)
    )
    assert store.run_steps(ns, SITE) == expected
    assert store.run_steps(ns, "other.test") == ()
    assert store.run_steps(f"{ns}x", SITE) == ()


def test_a_skill_is_one_hop_for_the_path_query(store, ns):
    """R-SKILL-3, R-SKILL-6: with a skill over the first 10 of 11 steps, the target is two hops
    away; the path comes back expanded, priced by the skill, and names the skill. Deleting the
    skill removes the shortcut and nothing else."""
    states, transitions = chain(store, ns, 11)
    target = transitions[-1]
    assert store.shortest_paths(ns, states[0].id, target.id, max_steps=8, path_count=3) == ()

    skill = skill_over(ns, transitions[:10], cost=1.25)
    store.upsert_skill(ns, skill)
    store.upsert_skill(ns, skill)
    assert store.list_skills(ns, SITE) == (skill,)
    (path,) = store.shortest_paths(ns, states[0].id, target.id, max_steps=2, path_count=3)
    assert [step.id for step in path.steps] == [t.id for t in transitions]
    assert path.skill_ids == (skill.id,)
    assert path.cost == pytest.approx(1.25 + target.cost)
    assert store.shortest_paths(ns, states[0].id, target.id, max_steps=1, path_count=3) == ()
    assert store.shortest_paths(f"{ns}x", states[0].id, target.id, max_steps=2, path_count=3) == ()

    store.delete_skill(ns, skill.id)
    store.delete_skill(ns, skill.id)
    assert store.list_skills(ns, SITE) == ()
    assert store.shortest_paths(ns, states[0].id, target.id, max_steps=8, path_count=3) == ()
    assert len(store.get_map(ns, SITE).transitions) == 11
    (plain,) = store.shortest_paths(ns, states[4].id, target.id, max_steps=8, path_count=3)
    assert (plain.skill_ids, len(plain.steps)) == ((), 7)


def test_cheaper_plain_route_beats_an_expensive_skill(store, ns):
    """R-PLAN-7: a skill is used when cheaper, not merely because it exists."""
    states, transitions = chain(store, ns, 4)
    target = transitions[-1]
    plain_cost = sum(t.cost for t in transitions)
    store.upsert_skill(ns, skill_over(ns, transitions[:3], cost=plain_cost * 10))
    paths = store.shortest_paths(ns, states[0].id, target.id, max_steps=8, path_count=3)
    assert paths[0].skill_ids == ()
    assert paths[0].cost == pytest.approx(plain_cost)
    assert len(paths) == 2 and paths[1].skill_ids != ()


def test_sleep_log_is_append_only_and_ordered(store, ns):
    """R-SKILL-6: log entries are kept oldest first, once per id, per site and namespace."""
    _, transitions = chain(store, ns, 3)
    skill = skill_over(ns, transitions, cost=1.0)

    def entry(kind: SleepLogKind, minutes: int) -> SleepLogEntry:
        return SleepLogEntry(
            id=stable_id(ns, "log", kind.value, skill.id),
            site=SITE,
            kind=kind,
            skill_id=skill.id,
            via=skill.via,
            evidence="driven in 3 runs",
            at=T0 + timedelta(minutes=minutes),
        )

    created, removed = entry(SleepLogKind.SKILL_CREATED, 1), entry(SleepLogKind.SKILL_REMOVED, 5)
    store.append_log(ns, removed)
    store.append_log(ns, created)
    store.append_log(ns, created)
    assert store.list_log(ns, SITE) == (created, removed)
    assert store.list_log(ns, "other.test") == ()
    assert store.list_log(f"{ns}x", SITE) == ()
