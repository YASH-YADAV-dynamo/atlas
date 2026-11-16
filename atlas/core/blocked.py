"""Blocked and error pages are never learned (SHIP_PLAN 4.3 B, C3).

A bot wall, a 403 or an error page is not a step of any route. `blocked_reason` says why a
snapshot looks like one (None for a real page, a login page included); the learner and the
tools then store no state, edge or goal link for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import LocateResult, Snapshot
from atlas.core.signature import state_from_snapshot, tokens

MAX_ERROR_PAGE_TOKENS = 5
BLOCKED_PATH_PARTS = ("/sorry", "/captcha", "/challenge", "/cdn-cgi/")
_TITLE_PREFIXES = (
    "403",
    "404",
    "access denied",
    "forbidden",
    "just a moment",
    "attention required",
    "are you a robot",
)
_ERROR_5XX = re.compile(r"error 5\d\d")


def _path(url: str) -> str:
    rest = url.split("://", 1)[-1]
    slash = rest.find("/")
    path = rest[slash:] if slash >= 0 else "/"
    return path.split("?", 1)[0].split("#", 1)[0].lower()


def blocked_reason(snapshot: Snapshot) -> str | None:
    if snapshot.status is not None and snapshot.status >= 400:
        return f"http_{snapshot.status}"
    path = _path(snapshot.url)
    for part in BLOCKED_PATH_PARTS:
        if part in path:
            return f"url_path:{part.strip('/')}"
    title = snapshot.title.strip().lower()
    errorish = title.startswith(_TITLE_PREFIXES) or _ERROR_5XX.match(title) is not None
    if title and errorish and len(tokens(snapshot)) <= MAX_ERROR_PAGE_TOKENS:
        return "error_title"
    return None


def unlearned_result(
    store: GraphStore, ns: str, site: str, snapshot: Snapshot, params: Mapping[str, str] | None, reason: str
) -> LocateResult:
    """Where a blocked page would be, without writing or matching anything."""
    state = state_from_snapshot(ns, site, snapshot, params)
    return LocateResult(state_id=state.id, known=store.get_state(ns, state.id) is not None, blocked=reason)
