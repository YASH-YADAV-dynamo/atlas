"""Locate: snapshot -> state id, remembering the state (ADR-0002, SHIP_PLAN 4.1).

An exact signature hit always wins. Otherwise, before a new state is stored:
- tolerant locate: a known page of the same site and URL template whose token set is nearly
  the same (a rotating banner, a late-rendered control) is the same page;
- URL-level fallback: a url-and-title snapshot resolves to the known state of that URL template
  with the most successful outgoing steps; a full snapshot whose URL template has only a
  url-only state resolves to that one.
A resolved variant is never stored and nothing is merged, so a wrong match costs one failed
step and cannot corrupt the map.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import LocateResult, Snapshot, State
from atlas.core.signature import fingerprint, is_entity_page, state_from_snapshot, tokens
from atlas.log import log_event

DEFAULT_MIN_JACCARD = 0.85
MIN_TOKENS = 8  # smaller pages are exact-only
MAX_SYMMETRIC_DIFFERENCE = 12
MIN_MARGIN = 0.05  # the best candidate must beat the second by this much
MAX_CANDIDATES = 20

_settings = {"min_jaccard": DEFAULT_MIN_JACCARD}

Matched = Literal["similar", "url"]


def configure(*, min_jaccard: float) -> None:
    """Set the tolerant-locate threshold (`LOCATE_MIN_JACCARD`)."""
    _settings["min_jaccard"] = min_jaccard


@dataclass(frozen=True)
class Resolution:
    """Where a snapshot is, before anything is written. `state` is the page as seen."""

    state: State
    tok: str
    state_id: int
    known: bool
    matched: Matched | None = None
    entity: bool = False  # a page for one subject: its URL shows a parameter value
    write_state: bool = False
    write_tok: bool = False

    def result(self) -> LocateResult:
        return LocateResult(state_id=self.state_id, known=self.known, matched=self.matched)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _similar(
    seen: frozenset[str], candidates: Sequence[tuple[int, str]], threshold: float
) -> tuple[int, float] | None:
    if len(seen) < MIN_TOKENS:
        return None
    scored: list[tuple[float, int, int]] = []
    for sid, tok in candidates[:MAX_CANDIDATES]:
        other = frozenset(tok.split())
        if len(other) < MIN_TOKENS:
            continue
        scored.append((_jaccard(seen, other), len(seen ^ other), sid))
    if not scored:
        return None
    scored.sort(key=lambda row: row[0], reverse=True)
    score, difference, sid = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if score < threshold or difference > MAX_SYMMETRIC_DIFFERENCE or score - runner_up < MIN_MARGIN:
        return None
    return sid, score


def _url_fallback(
    store: GraphStore,
    ns: str,
    site: str,
    snapshot: Snapshot,
    params: Mapping[str, str] | None,
    candidates: Sequence[tuple[int, str]],
) -> int | None:
    if not candidates:
        return None
    if snapshot.elements:
        url_only = state_from_snapshot(ns, site, snapshot.model_copy(update={"elements": ()}), params).id
        return url_only if all(sid == url_only for sid, _ in candidates) else None
    ids = {sid for sid, _ in candidates}
    wins: Counter[int] = Counter()
    for transition in store.get_map(ns, site).transitions:
        if transition.from_state in ids:
            wins[transition.from_state] += transition.ok
    ranked = [sid for sid, _ in candidates if wins[sid] > 0]
    return max(ranked, key=lambda sid: wins[sid]) if ranked else None  # max keeps the newest on ties


def resolve(
    store: GraphStore, ns: str, site: str, snapshot: Snapshot, params: Mapping[str, str] | None = None
) -> Resolution:
    """Find the snapshot's state without writing anything (read-only keys use this alone)."""
    state = state_from_snapshot(ns, site, snapshot, params)
    entity = is_entity_page(snapshot, params, site)
    tok = "" if entity else fingerprint(tokens(snapshot))
    seen = frozenset(tok.split())
    if store.get_state(ns, state.id) is not None:
        backfill = False
        if len(seen) >= MIN_TOKENS:  # only fingerprints that can ever be compared are worth a read
            stored = dict(store.states_by_url(ns, site, state.url_tmpl))
            backfill = not stored.get(state.id)
        return Resolution(state, tok, state.id, known=True, entity=entity, write_tok=backfill)
    candidates = [(sid, t) for sid, t in store.states_by_url(ns, site, state.url_tmpl) if sid != state.id]
    if snapshot.elements and not entity:
        match = _similar(seen, candidates, _settings["min_jaccard"])
        if match is not None:
            log_event("locate_similar", seen_state=state.id, state=match[0], score=round(match[1], 4))
            return Resolution(state, tok, match[0], known=True, matched="similar")
    if not entity:
        fallback = _url_fallback(store, ns, site, snapshot, params, candidates)
        if fallback is not None:
            return Resolution(state, tok, fallback, known=True, matched="url")
    return Resolution(state, tok, state.id, known=False, entity=entity, write_state=True, write_tok=True)


def commit(store: GraphStore, ns: str, resolution: Resolution) -> LocateResult:
    """Write what `resolve` found missing: a new state and its fingerprint, or a backfill."""
    known = resolution.known
    if resolution.write_state:
        known = store.upsert_state(ns, resolution.state)
    if resolution.write_tok:
        store.set_state_tokens(ns, resolution.state.id, resolution.tok)
    return LocateResult(state_id=resolution.state_id, known=known, matched=resolution.matched)


def locate(
    store: GraphStore, ns: str, site: str, snapshot: Snapshot, params: Mapping[str, str] | None = None
) -> LocateResult:
    return commit(store, ns, resolve(store, ns, site, snapshot, params))
