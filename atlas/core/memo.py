"""The answer memo (SHIP_PLAN 6, D6): addresses of pages, never their content.

Three kinds of `Memo` per (namespace, site), told apart by `Memo.key`:

| key | url | use |
|---|---|---|
| `a:<goal id>:<params key>` | the answer page of a read goal for one parameter set | exact repeat: open it |
| `t:<goal id>` | the same address with parameter values as `{slot}` | new subject: bind and open it |
| `s` | the site's search results address with the query as `{q}` | new question: the agent binds it |

A template is stored only when binding the very parameters it was made from gives the same
address back, so a page that does not follow the pattern never produces a wrong guess. A bound
value is URL-quoted inside one path segment or one query value, so it cannot change the host.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlsplit, urlunsplit

from atlas.contracts.ports import Clock, MemoStore
from atlas.contracts.types import Memo
from atlas.core.ids import memo_id

MAX_URL = 2048
SEARCH_KEY = "s"
SEARCH_SLOT = "{q}"
_JOINERS = re.compile(r"[\s_+-]+")
_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SEARCH_QUERY_KEYS = frozenset({"q", "s", "k", "p", "query", "search", "keyword", "keywords", "term", "text"})
_PATH_SAFE = "!$'()*,;:@~"


def _fold(text: str) -> str:
    return _JOINERS.sub(" ", unquote_plus(text)).strip().lower()


def _values(params: Mapping[str, str]) -> dict[str, str]:
    """Folded parameter value -> slot (first slot in name order wins)."""
    found: dict[str, str] = {}
    for slot in sorted(params):
        folded = _fold(params[slot])
        if folded:
            found.setdefault(folded, slot)
    return found


def params_key(params: Mapping[str, str]) -> str:
    """The same subject written in another case or spacing is the same key."""
    canon = "\n".join(f"{slot}={_fold(params[slot])}" for slot in sorted(params))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def answer_key(goal: int, params: Mapping[str, str]) -> str:
    return f"a:{goal}:{params_key(params)}"


def template_key(goal: int) -> str:
    return f"t:{goal}"


def clean_url(url: str) -> str | None:
    """The address as stored: http(s) only, no credentials, no fragment, bounded. None if unusable."""
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None
    return cleaned if len(cleaned) <= MAX_URL else None


def _swap_query(query: str, values: Mapping[str, str], *, literal: str | None = None) -> tuple[str, bool]:
    """Raw query with parameter values turned into `{slot}` (or, with `literal`, only the first
    one, into that text)."""
    out: list[str] = []
    hit = False
    for piece in query.split("&") if query else []:
        key, eq, raw = piece.partition("=")
        slot = values.get(_fold(raw)) if eq and raw else None
        if slot is not None and (literal is None or not hit):
            out.append(f"{key}={literal if literal is not None else '{' + slot + '}'}")
            hit = True
        else:
            out.append(piece)
    return "&".join(out), hit


def _swap_segment(segment: str, values: Mapping[str, str]) -> str:
    """One path segment with a parameter value as `{slot}`: the whole segment, or the value as
    whole words inside it (`List_of_awards_received_by_Hrithik_Roshan` -> `..._by_{person}`)."""
    folded = _fold(segment)
    if folded in values:
        return "{" + values[folded] + "}"
    text = unquote(segment)
    for value in sorted(values, key=len, reverse=True):
        words = value.split(" ")
        if len(words) < 2 and len(value) < 4:
            continue  # a short single word would match too much
        body = r"[\s_+-]+".join(re.escape(w) for w in words)
        found = re.search(rf"(?<![^\s_+-]){body}(?![^\s_+-])", text, re.IGNORECASE)
        if found:
            return text[: found.start()] + "{" + values[value] + "}" + text[found.end() :]
    return segment


def answer_template(url: str, params: Mapping[str, str]) -> str | None:
    """`url` with parameter values as `{slot}`, or None when none appears or binding would differ."""
    values = _values(params)
    if not values:
        return None
    parts = urlsplit(url)
    swapped = [_swap_segment(seg, values) for seg in parts.path.split("/")]
    query, _ = _swap_query(parts.query, values)
    template = urlunsplit((parts.scheme, parts.netloc, "/".join(swapped), query, ""))
    if not _SLOT.search(template):
        return None
    bound = bind(template, params)
    same = bound is not None and unquote(bound).lower() == unquote(url).lower()
    return template if same else None


def bind(template: str, params: Mapping[str, str]) -> str | None:
    """The template with each `{slot}` replaced by the quoted value; None if a value is missing."""
    parts = urlsplit(template)
    missing = False

    def fill(match: re.Match[str], in_path: bool) -> str:
        nonlocal missing
        value = params.get(match.group(1), "").strip()
        if not value:
            missing = True
            return ""
        if in_path:
            return quote(value.replace(" ", "_"), safe=_PATH_SAFE)
        return quote_plus(value, safe="")

    path = _SLOT.sub(lambda m: fill(m, True), parts.path)
    query = _SLOT.sub(lambda m: fill(m, False), parts.query)
    if missing:
        return None
    return clean_url(urlunsplit((parts.scheme, parts.netloc, path, query, "")))


def search_template(url: str, params: Mapping[str, str], site: str) -> str | None:
    """A results address of this site whose search box value is a parameter: `...?search={q}`."""
    values = _values(params)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not values or not (host == site or host.endswith("." + site) or site.endswith("." + host)):
        return None
    for piece in parts.query.split("&"):
        key = piece.partition("=")[0].lower()
        if key in _SEARCH_QUERY_KEYS or "search" in key or "query" in key:
            query, hit = _swap_query(parts.query, values, literal=SEARCH_SLOT)
            if hit:
                template = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
                return template if len(template) <= MAX_URL else None
    return None


def _put(store: MemoStore, clock: Clock, ns: str, site: str, key: str, url: str, title: str) -> None:
    store.put_memo(
        ns, Memo(id=memo_id(ns, site, key), site=site, key=key, url=url, title=title[:200], at=clock.now())
    )


def remember_answer(
    store: MemoStore,
    clock: Clock,
    ns: str,
    site: str,
    goal: int,
    params: Mapping[str, str],
    url: str,
    title: str = "",
) -> bool:
    """Remember the answer page of a read goal (and the goal's URL template). False if unusable."""
    address = clean_url(url)
    if address is None:
        return False
    _put(store, clock, ns, site, answer_key(goal, params), address, title)
    template = answer_template(address, params)
    if template is not None:
        _put(store, clock, ns, site, template_key(goal), template, "")
    return True


def remember_search(
    store: MemoStore, clock: Clock, ns: str, site: str, urls: Sequence[str], params: Mapping[str, str]
) -> bool:
    """Remember the site's search entry from the first of `urls` that shows a parameter in a search box."""
    for url in urls:
        address = clean_url(url)
        template = search_template(address, params, site) if address else None
        if template is not None:
            _put(store, clock, ns, site, SEARCH_KEY, template, "")
            return True
    return False


def recall(
    store: MemoStore, ns: str, site: str, goals: Sequence[int], params: Mapping[str, str]
) -> tuple[str, int] | None:
    """(address, index into `goals`) of the remembered answer: an exact repeat first, then the
    URL template bound to `params`. None when nothing is remembered."""
    for index, goal in enumerate(goals):
        memo = store.get_memo(ns, memo_id(ns, site, answer_key(goal, params)))
        if memo is not None:
            return memo.url, index
    for index, goal in enumerate(goals):
        memo = store.get_memo(ns, memo_id(ns, site, template_key(goal)))
        bound = bind(memo.url, params) if memo is not None else None
        if bound is not None:
            return bound, index
    return None


def search_entry(store: MemoStore, ns: str, site: str) -> str | None:
    memo = store.get_memo(ns, memo_id(ns, site, SEARCH_KEY))
    return memo.url if memo is not None else None
