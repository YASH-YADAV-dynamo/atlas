"""Locate: snapshot -> state id, remembering the state."""

from __future__ import annotations

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import LocateResult, Snapshot
from atlas.core.signature import state_from_snapshot


def locate(store: GraphStore, ns: str, site: str, snapshot: Snapshot) -> LocateResult:
    state = state_from_snapshot(ns, site, snapshot)
    known = store.upsert_state(ns, state)
    return LocateResult(state_id=state.id, known=known)
