"""Small builders shared by the test layers. They only use public contract types and
the public core functions listed in docs/architecture.md.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import (
    Action,
    ActionKind,
    Element,
    Locator,
    ReportEvent,
    ReportResult,
    Snapshot,
)

SITE = "shop.test"
GOAL = "finish the job"
_event_numbers = itertools.count(1)


class FixedClock:
    """A Clock port that always returns the same instant."""

    def __init__(self, at: datetime | None = None) -> None:
        self._at = at or datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._at


class TickClock:
    """A Clock port that moves one second forward on every call, so event order is defined."""

    def __init__(self, start: datetime | None = None) -> None:
        self._next = start or datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        current = self._next
        self._next = current + timedelta(seconds=1)
        return current


def new_event_id() -> str:
    return f"evt-{next(_event_numbers):08d}"


def page(path: str, *controls: str, rows: int = 0) -> Snapshot:
    """A snapshot at `path` with fixed controls given as "role:name" and `rows` list rows."""
    fixed = [Element(role=c.split(":", 1)[0], name=c.split(":", 1)[1]) for c in controls]
    listed = [Element(role="link", name=f"Row {i}", in_list=True) for i in range(rows)]
    return Snapshot(url=f"https://{SITE}{path}", title=path, elements=(*fixed, *listed))


def click(name: str, role: str = "button") -> Action:
    return Action(kind=ActionKind.CLICK, locator=Locator(role=role, name=name))


def learn(
    store: GraphStore,
    ns: str,
    source: Snapshot,
    action: Action,
    target: Snapshot,
    *,
    ms: int = 100,
    ok: bool = True,
    expect_state: int | None = None,
    goal_text: str | None = None,
    goal_reached: bool = False,
    params: dict[str, str] | None = None,
    event_id: str | None = None,
    site: str = SITE,
    run_id: str | None = None,
    clock: object = None,
) -> ReportResult:
    """Locate `source`, then report `action` from it, exactly as an API caller would."""
    from atlas.core.learner import report
    from atlas.core.locate import locate

    clock = clock or FixedClock()
    start = locate(store, ns, site, source)
    event = ReportEvent(
        site=site,
        from_state=start.state_id,
        action=action,
        to_snapshot=target,
        ok=ok,
        ms=ms,
        event_id=event_id or new_event_id(),
        expect_state=expect_state,
        goal_text=goal_text,
        goal_reached=goal_reached,
        params=params or {},
        run_id=run_id,
    )
    return report(store, clock, ns, event)


def state_id_of(store: GraphStore, ns: str, snapshot: Snapshot, site: str = SITE) -> int:
    from atlas.core.locate import locate

    return locate(store, ns, site, snapshot).state_id
