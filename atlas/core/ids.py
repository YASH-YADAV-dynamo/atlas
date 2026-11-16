"""Stable 53-bit ids (ADR-0002). Every id carries a kind tag and the namespace."""

from __future__ import annotations

import hashlib

from atlas.contracts.types import MAX_ID, Action

_SEP = "\x1f"


def stable_id(*parts: str | int) -> int:
    """SHA-256 of the parts reduced to [1, MAX_ID]."""
    digest = hashlib.sha256(_SEP.join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % MAX_ID + 1


def state_id(ns: str, site: str, sig: str) -> int:
    return stable_id("state", ns, site, sig)


def action_aid(action: Action) -> str:
    """Content hash of an action (its canonical JSON)."""
    return hashlib.sha256(action.model_dump_json().encode("utf-8")).hexdigest()


def transition_id(ns: str, from_state: int, action: Action, to_state: int) -> int:
    return stable_id("act", ns, from_state, action_aid(action), to_state)


def goal_id(ns: str, site: str, goal_key: str) -> int:
    return stable_id("goal", ns, site, goal_key)


def event_node_id(ns: str, event_id: str) -> int:
    return stable_id("event", ns, event_id)


def entity_id(ns: str, site: str, entity_type: str, key: str) -> int:
    return stable_id(ns, "entity", site, entity_type, key)


def change_id(ns: str, transition_id: int) -> int:
    """One change event per transition: re-detection updates the same event."""
    return stable_id(ns, "change", transition_id)


def skill_id(ns: str, via: tuple[int, ...]) -> int:
    """A skill is named by the transitions it compresses."""
    return stable_id(ns, "skill", *via)


def sleep_log_id(ns: str, skill_id: int, seq: int) -> int:
    """The `seq`-th sleep-log entry about a skill (0 for its first creation)."""
    return stable_id(ns, "log", skill_id, seq)
