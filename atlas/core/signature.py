"""State signature (ADR-0002), plus the token fingerprint tolerant locate compares."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, unquote_plus, urlsplit

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
_WORD_JOINERS = re.compile(r"[\s_+-]+")
_ID_VALUE = re.compile(r"[\d\s.,:/#]+")
MAX_FINGERPRINT_TOKENS = 400


def _is_id(segment: str) -> bool:
    return _DIGITS.search(segment) is not None or _OPAQUE_ID.fullmatch(segment) is not None


def _fold(text: str) -> str:
    """Case-insensitive comparison form: URL-decoded, `_`, `+`, `-` and spaces as one space."""
    return _WORD_JOINERS.sub(" ", unquote_plus(text)).strip().lower()


def _value_slots(params: Mapping[str, str] | None) -> dict[str, str]:
    """Folded name-like parameter values -> slot. Id-like values (order numbers, opaque ids) are
    left out: their URL segment is already `{}` and their page keeps token identity, so a
    changed control on an order page is still seen as a change."""
    slots: dict[str, str] = {}
    for slot, value in sorted((params or {}).items()):
        folded = _fold(value)
        if folded and not _ID_VALUE.fullmatch(folded) and not _OPAQUE_ID.fullmatch(folded):
            slots.setdefault(folded, slot)
    return slots


def _off_site_host(host: str | None, site: str | None) -> str | None:
    """The URL's host when it is not the site's own (only for sites named as a domain)."""
    if not host or not site or "." not in site:
        return None
    host = host.lower()
    if host == site or host.endswith("." + site) or site.endswith("." + host):
        return None
    return host


def template_of(
    url: str, params: Mapping[str, str] | None = None, site: str | None = None
) -> tuple[str, bool]:
    """(`url_template`, whether a parameter value was replaced by `{slot}`)."""
    parts = urlsplit(url)
    slots = _value_slots(params)
    slotted = False

    def slot_for(text: str) -> str | None:
        return slots.get(_fold(text)) if text else None

    def segment(seg: str) -> str:
        nonlocal slotted
        slot = slot_for(seg)
        if slot is not None:
            slotted = True
            return "{" + slot + "}"
        return "{}" if _is_id(seg) else seg

    path = "/".join(segment(seg) for seg in parts.path.split("/")).rstrip("/") or "/"
    keys: dict[str, str] = {}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if not value or key in PAGINATION_KEYS:
            continue
        slot = slot_for(value)
        if slot is not None:
            slotted = True
            keys[key] = f"{key}={{{slot}}}"
        else:
            keys.setdefault(key, key)
    query = "&".join(keys[key] for key in sorted(keys))
    host = _off_site_host(parts.hostname, site)
    tmpl = f"//{host}{path}" if host else path
    return (f"{tmpl}?{query}" if query else tmpl), slotted


def url_template(url: str, params: Mapping[str, str] | None = None, site: str | None = None) -> str:
    """Path with digit-bearing segments as `{}`, query reduced to its sorted non-empty keys.

    A name-like path segment or query value equal to a parameter value (case-insensitive; space
    as `_`, `+`, `-` or `%20`) becomes `{slot}`. A host other than `site` is kept as `//host`.
    """
    return template_of(url, params, site)[0]


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


def tokens(snapshot: Snapshot) -> frozenset[str]:
    """The role:name tokens the signature is built from."""
    return frozenset(token for element in snapshot.elements if (token := _token(element)) is not None)


def fingerprint(token_set: frozenset[str]) -> str:
    """Each token as the first 8 hex characters of its sha256, sorted and space-joined. No text."""
    hashed = sorted({hashlib.sha256(t.encode("utf-8")).hexdigest()[:8] for t in token_set})
    return " ".join(hashed[:MAX_FINGERPRINT_TOKENS])


def signature(snapshot: Snapshot, params: Mapping[str, str] | None = None, site: str | None = None) -> str:
    """sha256 over the URL template and the token set. An entity page (a parameter value in its
    URL) is identified by its slotted template alone, so every subject is one state."""
    tmpl, slotted = template_of(snapshot.url, params, site)
    lines = [VERSION, tmpl] if slotted else [VERSION, tmpl, *sorted(tokens(snapshot))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def is_entity_page(
    snapshot: Snapshot, params: Mapping[str, str] | None = None, site: str | None = None
) -> bool:
    return template_of(snapshot.url, params, site)[1]


def state_from_snapshot(
    ns: str, site: str, snapshot: Snapshot, params: Mapping[str, str] | None = None
) -> State:
    sig = signature(snapshot, params, site)
    return State(
        id=state_id(ns, site, sig),
        site=site,
        sig=sig,
        url_tmpl=url_template(snapshot.url, params, site),
        title=snapshot.title,
    )
