"""Form fills reported on their own, held until the trigger they belong to (ADR-0003).

Agents in planner mode often report "type into the search box" and "click Search" as two steps.
The fill does not change the page, so stored as its own step it would be a self-loop that no
shortest path ever takes: a replayed route would click Search with an empty box. Instead the
fill waits here and is folded into the next action reported from the same page.

Hosted MCP tools are stateless per call, so the buffer is process-wide, keyed by namespace,
site, page state and run id, bounded in size and age.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from atlas.contracts.types import Action, ActionKind, Fill, ReportEvent
from atlas.core.signature import state_from_snapshot

FILL_KINDS = frozenset({ActionKind.FILL, ActionKind.SELECT, ActionKind.CHECK})
MAX_FILLS = 20  # Action.fills max_length
_TTL_SECONDS = 15 * 60
_MAX_KEYS = 10_000

Key = tuple[str, str, int, str]


def as_fill(action: Action) -> Fill | None:
    """The action as a standalone form fill, or None if it is not one (or carries fills)."""
    if action.locator is None or action.fills:
        return None
    if action.kind is ActionKind.FILL:
        kind = ActionKind.FILL
    elif action.kind is ActionKind.SELECT:
        kind = ActionKind.SELECT
    elif action.kind is ActionKind.CHECK:
        kind = ActionKind.CHECK
    else:
        return None
    if action.param is None and action.value is None:
        if kind is not ActionKind.CHECK:
            return None
        return Fill(locator=action.locator, kind=kind, value="true")  # a bare check means "checked"
    return Fill(locator=action.locator, kind=kind, param=action.param, value=action.value)


class PendingFills:
    def __init__(self, ttl: float = _TTL_SECONDS, max_keys: int = _MAX_KEYS) -> None:
        self._ttl = ttl
        self._max = max_keys
        self._lock = threading.Lock()
        self._items: OrderedDict[Key, tuple[float, list[Fill]]] = OrderedDict()

    def add(self, key: Key, fill: Fill) -> None:
        """Remember a fill; a later fill of the same control replaces the earlier one."""
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            _, fills = self._items.pop(key, (now, []))
            fills = [f for f in fills if _where(f) != _where(fill)] + [fill]
            self._items[key] = (now, fills[-MAX_FILLS:])
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def take(self, key: Key) -> list[Fill]:
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            entry = self._items.pop(key, None)
        return entry[1] if entry else []

    def _expire(self, now: float) -> None:
        while self._items:
            first = next(iter(self._items))
            if now - self._items[first][0] <= self._ttl:
                return
            del self._items[first]


def _where(fill: Fill) -> str:
    return fill.locator.model_dump_json()


def fold(action: Action, fills: list[Fill]) -> Action:
    """The action with pending fills first; fills the action already carries win per control."""
    if not fills:
        return action
    own = {_where(f) for f in action.fills}
    if action.locator is not None and action.kind in FILL_KINDS:
        own.add(action.locator.model_dump_json())  # the trigger itself sets this control
    merged = tuple(f for f in fills if _where(f) not in own) + action.fills
    return action.model_copy(update={"fills": merged[-MAX_FILLS:]})


PENDING = PendingFills()


def hold_or_fold(ns: str, event: ReportEvent) -> ReportEvent | None:
    """None when the report is a same-page form fill, now held for the next action from that page;
    otherwise the event, with any held fills folded into its action when it succeeded."""
    key = (ns, event.site, event.from_state, event.run_id or "")
    fill = as_fill(event.action) if event.ok and not event.goal_reached else None
    if fill is not None and state_from_snapshot(ns, event.site, event.to_snapshot).id == event.from_state:
        PENDING.add(key, fill)
        return None
    if not event.ok:
        return event
    held = PENDING.take(key)
    return event.model_copy(update={"action": fold(event.action, held)}) if held else event
