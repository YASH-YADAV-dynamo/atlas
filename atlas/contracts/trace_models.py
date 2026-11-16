"""Trace import contract (SPEC 3b.3): recorded agent runs as JSON lines, one step per line.

A line is one `TraceStep` as JSON. This is the schema a platform converts its step recordings
to; importing it replays the steps through the Route API, so the map learns without re-exploring.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from atlas.contracts.types import Action, EventId, Frozen, GoalText, Params, Site, Snapshot

TRACE_EVENT_PREFIX = "trace"


class TraceStep(Frozen):
    """One recorded step: the page before, the action, the page after and whether it worked.

    `action` holds the concrete values used; with `params` given, the import replaces them
    by `{slot}` placeholders, so the learned route serves other parameter values.
    `goal_reached` on a run's last step links `goal_text` to it.
    """

    run_id: EventId
    index: Annotated[int, Field(ge=0, le=9999)]
    site: Site
    before: Snapshot
    action: Action
    after: Snapshot
    ok: bool = True
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0
    goal_text: GoalText | None = None
    params: Params = Field(default_factory=dict)
    goal_reached: bool = False


class ImportSummary(Frozen):
    """What an import did. `duplicates` are steps the map had already recorded (re-import)."""

    steps: Annotated[int, Field(ge=0)]
    runs: Annotated[int, Field(ge=0)]
    new_states: Annotated[int, Field(ge=0)]
    duplicates: Annotated[int, Field(ge=0)]
    failed_steps: Annotated[int, Field(ge=0)]
    goals_linked: Annotated[int, Field(ge=0)]


def trace_event_id(step: TraceStep) -> str:
    """The event id an imported step is reported under: stable, so re-imports count once."""
    return f"{TRACE_EVENT_PREFIX}-{step.run_id[:60]}-{step.index:04d}"
