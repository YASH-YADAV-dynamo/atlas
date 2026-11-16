"""Snapshot normaliser (SHIP_PLAN 4.3 A1, A2, A3, A9): truncate, never reject.

Runs before validation on every snapshot input (REST and MCP). Oversized or sloppy input is
cut down to the contract limits so it never raises a validation error: interactive roles only,
exact duplicates dropped, the first 400 elements in page order, names cut to 200 characters
(whitespace collapsed, so the signature sees one deterministic name), text to 4000, title to 200,
a missing URL scheme filled with `https://` and the host lowercased. A URL over 2048 characters
is still rejected: it is the page's identity.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, cast
from urllib.parse import urlsplit, urlunsplit

from pydantic import BeforeValidator

from atlas.contracts.types import Snapshot

MAX_ELEMENTS = 400
MAX_NAME = 200
MAX_TEXT = 4000
MAX_TITLE = 200
INTERACTIVE_ROLES = frozenset(
    {
        "link",
        "button",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "switch",
        "spinbutton",
        "slider",
        "listbox",
    }
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SPACES = re.compile(r"\s+")


def normalise_url(url: str) -> str:
    """`https://` when the scheme is missing, host lowercased. Path-only URLs are left alone."""
    url = url.strip()
    if not url or (url.startswith("/") and not url.startswith("//")):
        return url
    if url.startswith("//"):
        url = "https:" + url
    elif not _SCHEME.match(url):
        url = "https://" + url
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if "@" in netloc:
            userinfo, _, hostport = netloc.rpartition("@")
            netloc = f"{userinfo}@{hostport.lower()}"
        else:
            netloc = netloc.lower()
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return url


def _element(raw: object) -> object:
    """The cleaned element; None when it is not an interactive control."""
    if not isinstance(raw, dict):
        return raw  # not ours to fix: validation reports it
    item = cast("dict[str, object]", raw)
    role = item.get("role")
    if not isinstance(role, str) or role.strip().lower() not in INTERACTIVE_ROLES:
        return None
    name = item.get("name", "")
    if isinstance(name, str):
        name = _SPACES.sub(" ", name).strip()[:MAX_NAME]
    return {**item, "role": role.strip().lower(), "name": name}


def normalise_raw(value: object) -> object:
    """`normalise` for anything a caller might send. Non-snapshot input is returned unchanged."""
    if isinstance(value, Snapshot):
        value = value.model_dump(exclude_none=True)
    if not isinstance(value, dict):
        return value
    out = dict(cast("dict[str, object]", value))
    url, title, text = out.get("url"), out.get("title"), out.get("text")
    if isinstance(url, str):
        out["url"] = normalise_url(url)
    if isinstance(title, str):
        out["title"] = title[:MAX_TITLE]
    if isinstance(text, str):
        out["text"] = text[:MAX_TEXT]
    status = out.get("status")
    if status is not None and not (
        isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599
    ):
        del out["status"]
    elements = out.get("elements")
    if isinstance(elements, (list, tuple)):
        kept: list[object] = []
        seen: set[tuple[object, object, bool]] = set()
        for raw in cast("Sequence[object]", elements):
            element = _element(raw)
            if element is None:
                continue
            if isinstance(element, dict):
                item = cast("dict[str, object]", element)
                key = (item["role"], item["name"], bool(item.get("in_list", False)))
                if key in seen:
                    continue
                seen.add(key)
            kept.append(cast(object, element))
            if len(kept) >= MAX_ELEMENTS:
                break
        out["elements"] = kept
    return out


def normalise(snapshot: Snapshot) -> Snapshot:
    """A snapshot cut down to the contract limits (see module docstring)."""
    return Snapshot.model_validate(normalise_raw(snapshot))


# A Snapshot field that accepts any size: the JSON schema stays `Snapshot`, input is normalised first.
LenientSnapshot = Annotated[Snapshot, BeforeValidator(normalise_raw)]
