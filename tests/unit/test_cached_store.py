"""CachedStore: reads are served from memory until a write touches the namespace."""

from __future__ import annotations

import threading

from atlas.adapters.cached import CachedStore


class Inner:
    def __init__(self) -> None:
        self.data: dict[tuple[str, int], str] = {}
        self.reads = 0
        self.gate: threading.Event | None = None

    def get_state(self, ns: str, sid: int) -> str | None:
        self.reads += 1
        value = self.data.get((ns, sid))
        if self.gate is not None:
            self.gate.wait(2)
        return value

    def upsert_state(self, ns: str, sid: int, value: str) -> None:
        self.data[(ns, sid)] = value

    def has_event(self, ns: str, eid: str) -> bool:
        self.reads += 1
        return False


def test_repeated_reads_hit_memory():
    inner = Inner()
    store = CachedStore(inner)
    inner.data[("a", 1)] = "x"
    assert [store.get_state("a", 1) for _ in range(5)] == ["x"] * 5
    assert inner.reads == 1


def test_a_write_invalidates_only_its_namespace():
    inner = Inner()
    store = CachedStore(inner)
    store.get_state("a", 1)
    store.get_state("b", 1)
    store.upsert_state("a", 1, "new")
    assert store.get_state("a", 1) == "new"  # re-read
    store.get_state("b", 1)  # still cached
    assert inner.reads == 3


def test_none_is_cached_and_cleared_by_a_write():
    inner = Inner()
    store = CachedStore(inner)
    assert store.get_state("a", 9) is None
    assert store.get_state("a", 9) is None
    assert inner.reads == 1
    store.upsert_state("a", 9, "v")
    assert store.get_state("a", 9) == "v"


def test_pass_through_reads_are_never_cached():
    inner = Inner()
    store = CachedStore(inner)
    store.has_event("a", "e")
    store.has_event("a", "e")
    assert inner.reads == 2


def test_a_read_racing_a_write_does_not_cache_stale_data():
    inner = Inner()
    store = CachedStore(inner)
    inner.data[("a", 1)] = "old"
    inner.gate = threading.Event()
    seen: list[str | None] = []
    reader = threading.Thread(target=lambda: seen.append(store.get_state("a", 1)))
    reader.start()  # reads "old", then blocks before storing it
    store.upsert_state("a", 1, "new")
    inner.gate.set()
    reader.join()
    inner.gate = None
    assert seen == ["old"]
    assert store.get_state("a", 1) == "new"


def test_entries_expire():
    inner = Inner()
    store = CachedStore(inner, ttl=0.0)
    store.get_state("a", 1)
    store.get_state("a", 1)
    assert inner.reads == 2
