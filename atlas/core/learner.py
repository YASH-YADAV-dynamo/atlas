"""Learner: turn a report into idempotent observations and goal links (ADR-0001)."""

from __future__ import annotations

from atlas.contracts.ports import Clock, GraphStore
from atlas.contracts.types import Goal, Observation, ReportEvent, ReportResult, Transition
from atlas.core.ids import goal_id, transition_id
from atlas.core.locate import locate
from atlas.core.params import goal_key, templatise_action

_MAX_EVENT_ID = 80
_EXP_SUFFIX = ":exp"


def _expected_event_id(event_id: str) -> str:
    return event_id[: _MAX_EVENT_ID - len(_EXP_SUFFIX)] + _EXP_SUFFIX


def report(store: GraphStore, clock: Clock, ns: str, event: ReportEvent) -> ReportResult:
    arrived = locate(store, ns, event.site, event.to_snapshot)
    action = templatise_action(event.action, event.params)
    expected_id = _expected_event_id(event.event_id)
    duplicate = store.has_event(ns, event.event_id) or store.has_event(ns, expected_id)
    now = clock.now()

    def observe(event_id: str, to_state: int, ok: bool) -> Transition:
        return store.record_observation(
            ns,
            Observation(
                event_id=event_id,
                transition_id=transition_id(ns, event.from_state, action, to_state),
                site=event.site,
                from_state=event.from_state,
                action=action,
                to_state=to_state,
                ok=ok,
                ms=event.ms,
                at=now,
                run_id=event.run_id,
            ),
        )

    reached = event.expect_state is None or event.expect_state == arrived.state_id
    recorded: list[Transition] = []
    achieved: Transition | None = None
    if event.ok and reached:
        achieved = observe(event.event_id, arrived.state_id, True)
        recorded.append(achieved)
    elif event.expect_state is not None:
        recorded.append(observe(expected_id, event.expect_state, False))
        if event.ok:
            achieved = observe(event.event_id, arrived.state_id, True)
            recorded.append(achieved)

    if event.goal_reached and achieved is not None and event.goal_text is not None:
        key = goal_key(event.goal_text, event.params)
        goal = Goal(id=goal_id(ns, event.site, key), site=event.site, text=key)
        store.link_goal(ns, goal, achieved.id)
    return ReportResult(
        state_id=arrived.state_id, known=arrived.known, duplicate=duplicate, transitions=tuple(recorded)
    )
