"""Flow-change detection (SPEC 5.5): a transition with a strong record that stopped working.

The rule is deliberately narrow so a flaky control is penalised (by its cost) but not announced:
the edge has at least `STRONG_OK` successes and its two newest outcomes are both failures.
"""

from __future__ import annotations

from collections.abc import Sequence

from atlas.contracts.ports import ChangeStore, Clock
from atlas.contracts.types import ChangeEvent, ReportResult, Transition
from atlas.core.ids import change_id

STRONG_OK = 5
FAILURES_IN_A_ROW = 2


def is_flow_change(transition: Transition, recent: Sequence[bool]) -> bool:
    """True when the transition is strong and its newest outcomes (newest first) all failed."""
    newest = list(recent[:FAILURES_IN_A_ROW])
    return transition.ok >= STRONG_OK and len(newest) == FAILURES_IN_A_ROW and not any(newest)


def _replacement(failed: Transition, result: ReportResult) -> Transition | None:
    """The transition recorded in the same report for the same action from the same state that
    led somewhere else: the road the site now takes instead."""
    for other in result.transitions:
        if (
            other.id != failed.id
            and other.from_state == failed.from_state
            and other.action == failed.action
            and other.to_state != failed.to_state
            and other.ok > 0
        ):
            return other
    return None


def note_report(store: ChangeStore, clock: Clock, ns: str, result: ReportResult) -> ChangeEvent | None:
    """Called after every report: record and return a change event when one is detected.

    Only transitions this report touched are examined. Re-recording an existing event is
    harmless (the store keeps `first_seen` and merges the replacement), so a dead edge that
    keeps failing keeps returning its event.
    """
    if result.duplicate:
        return None
    found: ChangeEvent | None = None
    for transition in result.transitions:
        if transition.ok < STRONG_OK or transition.fail < FAILURES_IN_A_ROW:
            continue  # cannot be a change: skip the history read
        recent = store.recent_outcomes(ns, transition.id, FAILURES_IN_A_ROW)
        if not is_flow_change(transition, recent):
            continue
        replacement = _replacement(transition, result)
        change = ChangeEvent(
            id=change_id(ns, transition.id),
            site=transition.site,
            transition_id=transition.id,
            from_state=transition.from_state,
            expected_state=transition.to_state,
            action=transition.action,
            first_seen=clock.now(),
            actual_state=replacement.to_state if replacement else None,
            replacement_id=replacement.id if replacement else None,
        )
        store.record_change(ns, change)
        found = found or change
    return found
