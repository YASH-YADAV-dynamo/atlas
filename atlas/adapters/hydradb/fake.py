"""In-memory GraphStore: same contract as the HydraDB adapter, for unit tests and local runs.

Counters are derived from the set of recorded events (ADR-0001), never incremented.
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from atlas.contracts.types import (
    ChangeEvent,
    Entity,
    Goal,
    MapView,
    Observation,
    RunStep,
    Skill,
    SleepLogEntry,
    State,
    StoredPath,
    Transition,
)
from atlas.core.cost import edge_cost


@dataclass(frozen=True)
class _Hop:
    """One hop of the path query: a transition, or a skill with its transitions expanded."""

    id: int
    from_state: int
    to_state: int
    cost: float
    steps: tuple[Transition, ...]
    skill_id: int | None = None


@dataclass
class _Namespace:
    states: dict[int, State] = field(default_factory=dict)
    events: dict[str, Observation] = field(default_factory=dict)
    shapes: dict[int, Observation] = field(default_factory=dict)  # first observation per transition
    evidence: dict[int, dict[str, Observation]] = field(default_factory=lambda: defaultdict(dict))
    goals: dict[int, list[int]] = field(default_factory=lambda: defaultdict(list))
    goal_nodes: dict[int, Goal] = field(default_factory=dict)
    entities: dict[int, Entity] = field(default_factory=dict)
    changes: dict[int, ChangeEvent] = field(default_factory=dict)
    skills: dict[int, Skill] = field(default_factory=dict)
    log: dict[int, SleepLogEntry] = field(default_factory=dict)


class InMemoryGraphStore:
    def __init__(self) -> None:
        self._spaces: defaultdict[str, _Namespace] = defaultdict(_Namespace)
        self._lock = threading.RLock()

    def upsert_state(self, ns: str, state: State) -> bool:
        with self._lock:
            states = self._spaces[ns].states
            known = state.id in states
            states[state.id] = state
            return known

    def get_state(self, ns: str, state_id: int) -> State | None:
        with self._lock:
            return self._spaces[ns].states.get(state_id)

    def has_event(self, ns: str, event_id: str) -> bool:
        with self._lock:
            return event_id in self._spaces[ns].events

    def record_observation(self, ns: str, obs: Observation) -> Transition:
        with self._lock:
            space = self._spaces[ns]
            original = space.events.get(obs.event_id)
            if original is not None:
                return self._transition(space, original.transition_id)
            space.events[obs.event_id] = obs
            space.shapes.setdefault(obs.transition_id, obs)
            space.evidence[obs.transition_id][obs.event_id] = obs
            return self._transition(space, obs.transition_id)

    def link_goal(self, ns: str, goal: Goal, transition_id: int) -> None:
        with self._lock:
            self._spaces[ns].goal_nodes[goal.id] = goal
            targets = self._spaces[ns].goals[goal.id]
            if transition_id not in targets:
                targets.append(transition_id)

    def goal_targets(self, ns: str, goal_id: int) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._spaces[ns].goals.get(goal_id, ()))

    def shortest_paths(
        self, ns: str, source: int, target: int, max_steps: int, path_count: int
    ) -> tuple[StoredPath, ...]:
        with self._lock:
            space = self._spaces[ns]
            if target not in space.shapes:
                return ()
            outgoing: defaultdict[int, list[_Hop]] = defaultdict(list)
            for hop in self._hops(space):
                outgoing[hop.from_state].append(hop)
            found: list[StoredPath] = []
            self._walk(outgoing, target, max_steps, source, [], {source}, found)
        found.sort(key=lambda path: (path.cost, [step.id for step in path.steps]))
        return tuple(found[:path_count])

    def _hops(self, space: _Namespace) -> list[_Hop]:
        hops: list[_Hop] = []
        for tid in space.shapes:
            t = self._transition(space, tid)
            hops.append(_Hop(id=tid, from_state=t.from_state, to_state=t.to_state, cost=t.cost, steps=(t,)))
        for skill in space.skills.values():
            if all(tid in space.shapes for tid in skill.via):
                steps = tuple(self._transition(space, tid) for tid in skill.via)
                hops.append(_Hop(skill.id, skill.from_state, skill.to_state, skill.cost, steps, skill.id))
        return hops

    def _walk(
        self,
        outgoing: defaultdict[int, list[_Hop]],
        target: int,
        max_steps: int,
        state: int,
        hops: list[_Hop],
        visited: set[int],
        found: list[StoredPath],
    ) -> None:
        for hop in outgoing.get(state, ()):
            if hop.id == target and hop.skill_id is None:
                chosen = (*hops, hop)
                found.append(
                    StoredPath(
                        steps=tuple(step for h in chosen for step in h.steps),
                        cost=sum(h.cost for h in chosen),
                        skill_ids=tuple(h.skill_id for h in chosen if h.skill_id is not None),
                    )
                )
            elif hop.to_state not in visited and len(hops) + 1 < max_steps:
                visited.add(hop.to_state)
                hops.append(hop)
                self._walk(outgoing, target, max_steps, hop.to_state, hops, visited, found)
                hops.pop()
                visited.discard(hop.to_state)

    def list_goals(self, ns: str, site: str) -> tuple[Goal, ...]:
        with self._lock:
            goals = (g for g in self._spaces[ns].goal_nodes.values() if g.site == site)
            return tuple(sorted(goals, key=lambda goal: goal.id))

    def get_transition(self, ns: str, transition_id: int) -> Transition | None:
        with self._lock:
            space = self._spaces[ns]
            return self._transition(space, transition_id) if transition_id in space.shapes else None

    def record_entity(self, ns: str, entity: Entity) -> None:
        with self._lock:
            self._spaces[ns].entities[entity.id] = entity

    def get_entity(self, ns: str, entity_id: int) -> Entity | None:
        with self._lock:
            return self._spaces[ns].entities.get(entity_id)

    def get_map(self, ns: str, site: str) -> MapView:
        with self._lock:
            space = self._spaces[ns]
            states = sorted((s for s in space.states.values() if s.site == site), key=lambda s: s.id)
            transitions = sorted(
                (self._transition(space, tid) for tid, shape in space.shapes.items() if shape.site == site),
                key=lambda t: t.id,
            )
            return MapView(site=site, states=tuple(states), transitions=tuple(transitions))

    # -- changes (ChangeStore) ------------------------------------------------------------

    def recent_outcomes(self, ns: str, transition_id: int, limit: int) -> tuple[bool, ...]:
        with self._lock:
            events = list(self._spaces[ns].evidence.get(transition_id, {}).values())
        # Newest first by event time; among equal times the later-recorded event is newer.
        ordered = sorted(enumerate(events), key=lambda pair: (pair[1].at, pair[0]), reverse=True)
        return tuple(event.ok for _, event in ordered[: max(0, limit)])

    def record_change(self, ns: str, change: ChangeEvent) -> None:
        with self._lock:
            changes = self._spaces[ns].changes
            existing = changes.get(change.id)
            if existing is not None:
                change = change.model_copy(
                    update={
                        "first_seen": existing.first_seen,
                        "actual_state": change.actual_state or existing.actual_state,
                        "replacement_id": change.replacement_id or existing.replacement_id,
                    }
                )
            changes[change.id] = change

    def list_changes(self, ns: str, site: str) -> tuple[ChangeEvent, ...]:
        with self._lock:
            found = [c for c in self._spaces[ns].changes.values() if c.site == site]
        return tuple(sorted(found, key=lambda c: (c.first_seen, c.id), reverse=True))

    def list_sites(self, ns: str) -> tuple[tuple[str, int, int], ...]:
        with self._lock:
            space = self._spaces[ns]
            states = Counter(s.site for s in space.states.values())
            edges = Counter(shape.site for shape in space.shapes.values())
            return tuple((site, states[site], edges[site]) for site in sorted(states))

    # -- skills (SkillStore) --------------------------------------------------------------

    def run_steps(self, ns: str, site: str) -> tuple[RunStep, ...]:
        """Ordered by (run_id, at); a failure sorts before a success recorded at the same
        instant, the order the learner writes them."""
        with self._lock:
            events = list(self._spaces[ns].events.values())
        steps = [
            RunStep(run_id=e.run_id, transition_id=e.transition_id, ok=e.ok, at=e.at)
            for e in events
            if e.run_id is not None and e.site == site
        ]
        return tuple(sorted(steps, key=lambda s: (s.run_id, s.at, s.ok)))

    def upsert_skill(self, ns: str, skill: Skill) -> None:
        with self._lock:
            self._spaces[ns].skills[skill.id] = skill

    def delete_skill(self, ns: str, skill_id: int) -> None:
        with self._lock:
            self._spaces[ns].skills.pop(skill_id, None)

    def list_skills(self, ns: str, site: str) -> tuple[Skill, ...]:
        with self._lock:
            found = [k for k in self._spaces[ns].skills.values() if k.site == site]
        return tuple(sorted(found, key=lambda k: k.id))

    def append_log(self, ns: str, entry: SleepLogEntry) -> None:
        with self._lock:
            self._spaces[ns].log.setdefault(entry.id, entry)

    def list_log(self, ns: str, site: str) -> tuple[SleepLogEntry, ...]:
        with self._lock:
            found = [e for e in self._spaces[ns].log.values() if e.site == site]
        return tuple(sorted(found, key=lambda e: (e.at, e.id)))

    @staticmethod
    def _transition(space: _Namespace, tid: int) -> Transition:
        shape = space.shapes[tid]
        events = space.evidence[tid].values()
        wins = [e for e in events if e.ok]
        ok, fail = len(wins), len(events) - len(wins)
        ms = sum(e.ms for e in wins) / ok if ok else 0.0
        return Transition(
            id=tid,
            site=shape.site,
            from_state=shape.from_state,
            to_state=shape.to_state,
            action=shape.action,
            ok=ok,
            fail=fail,
            ms=ms,
            cost=edge_cost(ok, fail, ms),
            last_ok_at=max((e.at for e in wins), default=None),
        )
