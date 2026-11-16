"""State signature (ADR-0002)."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlsplit

from atlas.contracts.types import Element, Snapshot, State
from atlas.core.ids import state_id

VERSION = "v1"
PAGINATION_KEYS = frozenset({"page", "p", "offset", "limit", "cursor", "per_page"})
PAGINATION_NAMES = frozenset({"next", "previous", "next page", "previous page", "first page", "last page"})
_DIGITS = re.compile(r"\d+")
_NUMBER = re.compile(r"\d+(?:[,.:/-]\d+)*")
_OPAQUE_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}", re.IGNORECASE
)
NUMBERED_PAGINATION_NAMES = frozenset({"{}", "page {}", "go to page {}"})
_SPACES = re.compile(r"\s+")


def _is_id(segment: str) -> bool:
    return _DIGITS.search(segment) is not None or _OPAQUE_ID.fullmatch(segment) is not None


def url_template(url: str) -> str:
    """Path with digit-bearing segments as `{}`, query reduced to its sorted non-empty keys."""
    parts = urlsplit(url)
    segments = ["{}" if _is_id(seg) else seg for seg in parts.path.split("/")]
    path = "/".join(segments).rstrip("/") or "/"
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    keys = sorted({k for k, v in pairs if v and k not in PAGINATION_KEYS})
    return f"{path}?{'&'.join(keys)}" if keys else path


def _token(element: Element) -> str | None:
    name = _SPACES.sub(" ", element.name.lower()).strip()
    if name in PAGINATION_NAMES:
        return None
    normalised = _NUMBER.sub("{}", name)
    if normalised in NUMBERED_PAGINATION_NAMES:
        return None
    if element.in_list:
        return f"{element.role}:*"
    return f"{element.role}:{normalised}"


def signature(snapshot: Snapshot) -> str:
    tokens = {token for element in snapshot.elements if (token := _token(element)) is not None}
    lines = [VERSION, url_template(snapshot.url), *sorted(tokens)]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def state_from_snapshot(ns: str, site: str, snapshot: Snapshot) -> State:
    sig = signature(snapshot)
    return State(
        id=state_id(ns, site, sig),
        site=site,
        sig=sig,
        url_tmpl=url_template(snapshot.url),
        title=snapshot.title,
    )
