"""Read-through cache in front of a MapStore, invalidated per namespace on every write.

A repeated task asks the same questions (which goals, which targets, which route from this
page) and HydraDB answers each with one or more round trips. Between two writes to a
namespace those answers cannot change, so they are served from memory.

Correctness rules:
- Any method that is not a known read is treated as a write and drops the namespace's
  entries (the namespace is the first argument of every store method), before and after it runs.
- A read stores its answer only if no write to that namespace started or finished meanwhile
  (generation check), so a racing write can never leave a stale answer behind.
- Entries also expire after a short TTL, which bounds staleness from writers in other
  processes (the hosted API runs one worker; seeding and the stdio server are separate).
"""

# ruff: noqa: ANN401  (a transparent proxy over any store method)
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

_CACHED_READS = frozenset(
    {
        "get_state",
        "goal_targets",
        "shortest_paths",
        "list_goals",
        "get_transition",
        "get_entity",
        "get_map",
        "list_sites",
        "list_skills",
        "list_changes",
    }
)
# Reads that must always see the latest data (idempotency checks, recent outcomes) or are cheap.
_PASS_THROUGH = frozenset({"has_event", "recent_outcomes", "run_steps", "list_log", "verify", "close"})
_ALL = "\0all"


class CachedStore:
    def __init__(self, inner: Any, *, ttl: float = 30.0, max_entries: int = 20_000) -> None:
        self._inner = inner
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[Any, ...], tuple[float, int, Any]] = OrderedDict()
        self._gen: dict[str, int] = {}
        self._writing: dict[str, int] = {}

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target) or name.startswith("_"):
            return target
        if name in _CACHED_READS:
            return self._cached(name, target)
        if name in _PASS_THROUGH:
            return target
        return self._write(target)

    # -- internals --------------------------------------------------------------------------

    def _ns_of(self, args: tuple[Any, ...]) -> str:
        return args[0] if args and isinstance(args[0], str) else _ALL

    def _bump(self, ns: str) -> None:
        """Drop the namespace's entries (all entries for an unknown namespace)."""
        with self._lock:
            if ns == _ALL:
                self._entries.clear()
                for key in self._gen:
                    self._gen[key] += 1
                self._gen[_ALL] = self._gen.get(_ALL, 0) + 1
                return
            self._gen[ns] = self._gen.get(ns, 0) + 1
            for key in [k for k in self._entries if k[0] == ns]:
                del self._entries[key]

    def _stamp(self, ns: str) -> tuple[int, int, int]:
        running = self._writing.get(ns, 0) + self._writing.get(_ALL, 0)
        return self._gen.get(ns, 0), self._gen.get(_ALL, 0), running

    def _write(self, target: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            ns = self._ns_of(args)
            with self._lock:
                self._writing[ns] = self._writing.get(ns, 0) + 1
            self._bump(ns)
            try:
                return target(*args, **kwargs)
            finally:
                with self._lock:
                    self._writing[ns] -= 1
                self._bump(ns)

        return call

    def _cached(self, name: str, target: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            ns = self._ns_of(args)
            try:
                key = (ns, name, args[1:], tuple(sorted(kwargs.items())))
                hash(key)
            except TypeError:
                return target(*args, **kwargs)
            now = time.monotonic()
            with self._lock:
                hit = self._entries.get(key)
                if hit is not None and now - hit[0] <= self._ttl:
                    self._entries.move_to_end(key)
                    return hit[2]
                before = self._stamp(ns)
            value = target(*args, **kwargs)
            with self._lock:
                after = self._stamp(ns)
                if before == after and after[2] == 0:  # no write began, ended or is running
                    self._entries[key] = (now, 0, value)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max:
                        self._entries.popitem(last=False)
            return value

        return call
