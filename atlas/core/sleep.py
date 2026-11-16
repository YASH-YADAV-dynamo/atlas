"""Sleep phase: compress proven paths into skills and remove broken ones (ADR-0006). No model."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from itertools import groupby
from typing import Protocol

from atlas.contracts.ports import ChangeStore, Clock, ContextStore, GraphStore, SkillStore
from atlas.contracts.types import RunStep, Skill, SleepLogEntry, SleepLogKind, SleepReport, Transition
from atlas.core.ids import skill_id, sleep_log_id

SKILL_DISCOUNT = 0.95  # a skill is priced just under its steps taken one by one
_BROKEN = (False, False)  # the two newest outcomes of a transition that breaks a skill


class SleepStore(GraphStore, ContextStore, ChangeStore, SkillStore, Protocol):
    """What the sleep phase reads and writes."""


def _segments(run: Sequence[RunStep], keep_out: frozenset[int]) -> list[tuple[int, ...]]:
    """Maximal stretches of successful steps, cut at failures and at keep-out transitions."""
    found: list[tuple[int, ...]] = []
    current: list[int] = []
    for step in run:
        if step.ok and step.transition_id not in keep_out:
            current.append(step.transition_id)
            continue
        if current:
            found.append(tuple(current))
        current = []
    if current:
        found.append(tuple(current))
    return found


def skill_candidates(
    runs: Sequence[Sequence[RunStep]], keep_out: frozenset[int], *, min_steps: int = 3, min_runs: int = 3
) -> tuple[tuple[tuple[int, ...], int], ...]:
    """(via, supporting runs) for every segment of at least `min_steps` transitions that occurs
    identically in at least `min_runs` runs, sorted by via."""
    support: defaultdict[tuple[int, ...], int] = defaultdict(int)
    for run in runs:
        for segment in set(_segments(run, keep_out)):
            if len(segment) >= min_steps:
                support[segment] += 1
    return tuple(sorted((via, count) for via, count in support.items() if count >= min_runs))


def _runs(steps: Sequence[RunStep]) -> list[list[RunStep]]:
    ordered = sorted(steps, key=lambda s: (s.run_id, s.at, s.ok))
    return [list(group) for _, group in groupby(ordered, key=lambda s: s.run_id)]


def _broken(store: SleepStore, ns: str, transition_id: int) -> bool:
    return store.recent_outcomes(ns, transition_id, len(_BROKEN)) == _BROKEN


def _log(store: SleepStore, clock: Clock, ns: str, kind: SleepLogKind, skill: Skill, evidence: str) -> None:
    # The sequence number is the skill's count of earlier entries: a repeated sleep run writes the
    # same id again (nothing new), while a removed and re-created skill logs each event.
    seq = sum(1 for entry in store.list_log(ns, skill.site) if entry.skill_id == skill.id)
    entry = SleepLogEntry(
        id=sleep_log_id(ns, skill.id, seq),
        site=skill.site,
        kind=kind,
        skill_id=skill.id,
        via=skill.via,
        evidence=evidence,
        at=clock.now(),
    )
    store.append_log(ns, entry)


def _new_skill(store: SleepStore, ns: str, site: str, via: tuple[int, ...], runs: int) -> Skill | None:
    steps: list[Transition] = []
    for tid in via:
        step = store.get_transition(ns, tid)
        if step is None or _broken(store, ns, tid):
            return None
        steps.append(step)
    return Skill(
        id=skill_id(ns, via),
        site=site,
        from_state=steps[0].from_state,
        to_state=steps[-1].to_state,
        via=via,
        runs=runs,
        cost=SKILL_DISCOUNT * sum(step.cost for step in steps),
    )


def run_sleep(store: SleepStore, clock: Clock, ns: str, site: str) -> SleepReport:
    """Remove skills with a broken step, then create a skill for every new candidate; log both."""
    removed: list[int] = []
    kept: set[int] = set()
    for skill in store.list_skills(ns, site):
        broken = [tid for tid in skill.via if _broken(store, ns, tid)]
        if not broken:
            kept.add(skill.id)
            continue
        store.delete_skill(ns, skill.id)
        _log(
            store,
            clock,
            ns,
            SleepLogKind.SKILL_REMOVED,
            skill,
            f"transition {broken[0]} failed twice in a row",
        )
        removed.append(skill.id)

    keep_out = frozenset(t for goal in store.list_goals(ns, site) for t in store.goal_targets(ns, goal.id))
    created: list[Skill] = []
    for via, runs in skill_candidates(_runs(store.run_steps(ns, site)), keep_out):
        if skill_id(ns, via) in kept:
            continue
        skill = _new_skill(store, ns, site, via, runs)
        if skill is None:
            continue
        store.upsert_skill(ns, skill)
        _log(store, clock, ns, SleepLogKind.SKILL_CREATED, skill, f"driven successfully in {runs} runs")
        created.append(skill)
    return SleepReport(site=site, created=tuple(created), removed=tuple(removed))
