"""Goal matching (SHIP_PLAN 5): exact key, same words, similar, and one-slot alignment.

Every surface (MCP, REST, SDK) resolves a goal here, so a reworded goal behaves the same way
everywhere. Nothing here guesses meaning: words are compared after a fixed normalisation, and
a match that could change something on the site is only ever offered, never used silently.

| Tier | Accepted automatically |
|---|---|
| exact (today's `goal_key`) | always |
| same words (stopwords and retrieval verbs dropped, plural `s` stripped, sorted) | read goals only |
| one-slot alignment ("please refund order 1005 for me" -> `refund order {order_id}`) | read goals only |
| similar (token Jaccard >= 0.6, same slots, clear winner) | never |

Read and act goals are told apart by an alias goal node the learner links on a reported
success: `~r <same-words key>` or `~a <same-words key>` (`alias_goal`). `~` goals are hidden
from listings (`visible_goals`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from atlas.contracts.ports import GraphStore
from atlas.contracts.types import Goal, GoalKind, GoalMatchKind
from atlas.core.ids import goal_id
from atlas.core.params import goal_key

ALIAS_PREFIX = "~"
MIN_SIMILAR = 0.6  # tier 3: token Jaccard
MIN_LEAD = 0.15  # tier 3: the best candidate's lead over the next one
MAX_SPAN = 6  # slot alignment: tokens a bound value may span
_MAX_GOAL_TEXT = 300

# Fixed list. Action verbs, negations, numbers, question words other than "what", and the
# particles that flip an action ("on", "off", "up", "down", "in", "out") are never here.
# fmt: off
STOPWORDS = frozenset(
    [
        "a", "an", "the", "of", "to", "for", "from", "by", "with", "about", "into", "onto", "at", "as",
        "and", "or", "but", "than", "so", "then", "please", "kindly", "me", "my", "mine", "i", "im",
        "i'm", "we", "us", "our", "ours", "you", "your", "yours", "it", "its", "this", "that", "these",
        "those", "there", "here", "their", "theirs", "them", "they", "he", "she", "him", "his", "her",
        "hers", "be", "is", "are", "was", "were", "been", "being", "am", "has", "have", "had", "do",
        "does", "did", "can", "could", "would", "will", "should", "shall", "may", "might", "must",
        "just", "also", "some", "any", "need", "want", "like", "help", "let", "lets", "let's", "go",
    ]
)
# fmt: on
RETRIEVAL_VERBS = frozenset({"find", "show", "get", "what", "list", "open"})

_TOKEN = re.compile(r"\{[a-z][a-z0-9_]*\}|[^\W_]+(?:'[^\W_]+)*")
_SLOT = re.compile(r"^\{[a-z][a-z0-9_]*\}$")


@dataclass(frozen=True)
class _Word:
    norm: str
    start: int
    end: int


def _norm(raw: str) -> str | None:
    """One token after normalisation, or None when it is a stopword or a retrieval verb."""
    if _SLOT.match(raw):
        return raw
    word = raw.lower()
    if word.endswith("'s"):
        word = word[:-2]
    if not word or word in STOPWORDS or word in RETRIEVAL_VERBS:
        return None
    if word.isalpha() and len(word) > 3:
        if word.endswith("ies"):
            word = word[:-3] + "y"
        elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
    return word


def _words(text: str) -> list[_Word]:
    out: list[_Word] = []
    for found in _TOKEN.finditer(text):
        norm = _norm(found.group(0))
        if norm is not None:
            out.append(_Word(norm, found.start(), found.end()))
    return out


def is_slot(token: str) -> bool:
    return bool(_SLOT.match(token))


def tokens(text: str) -> list[str]:
    """Normalised tokens in text order (slots kept)."""
    return [word.norm for word in _words(text)]


def content_words(text: str) -> frozenset[str]:
    """Normalised words without slots: what `find_skills` ranks on."""
    return frozenset(t for t in tokens(text) if not is_slot(t))


def same_words_key(text: str) -> str:
    """Tier 2 key: normalised tokens, sorted (word order is lost on purpose)."""
    return " ".join(sorted(tokens(text)))


def alias_goal(ns: str, site: str, same_words: str, kind: GoalKind) -> Goal:
    text = f"{ALIAS_PREFIX}{kind[0]} {same_words}"[:_MAX_GOAL_TEXT]
    return Goal(id=goal_id(ns, site, text), site=site, text=text)


def is_alias(goal: Goal) -> bool:
    return goal.text.startswith(ALIAS_PREFIX)


def visible_goals(goals: Iterable[Goal]) -> tuple[Goal, ...]:
    return tuple(goal for goal in goals if not is_alias(goal))


def skill_match(query: str, goal_text: str) -> int:
    """Shared content words when the goal is a fair answer to the query, else 0.

    At least one shared word and at least half of the query's content words (SHIP_PLAN 4.3 A5).
    """
    wanted = content_words(query)
    shared = len(wanted & content_words(goal_text))
    return shared if shared and 2 * shared >= len(wanted) else 0


@dataclass(frozen=True)
class GoalMatch:
    """How a goal text resolved. `automatic` means a route may be served for it."""

    targets: tuple[int, ...] = ()
    match: GoalMatchKind | None = None
    goal: str | None = None  # the stored goal, when it is not the caller's own key
    params: dict[str, str] = field(default_factory=dict[str, str])  # the values to plan with
    inferred: bool = False  # `params` were read out of the goal text (slot alignment)
    automatic: bool = False


_UNKNOWN = GoalMatch()


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _strong(same_words: str) -> bool:
    """A same-words key worth matching automatically: two tokens, one of them a real word.
    ("open order" and "open orders" both reduce to `order`; that is too little to trust.)"""
    parts = same_words.split()
    return len(parts) >= 2 and any(not is_slot(p) for p in parts)


def _align(text: str, stored: str) -> tuple[str, str] | None:
    """Bind the one slot of `stored` to a span of 1..MAX_SPAN tokens of `text`."""
    target = tokens(stored)
    slots = [i for i, t in enumerate(target) if is_slot(t)]
    if len(slots) != 1 or len(target) < 3:
        return None  # one anchoring word is too little: "open order {id}" would bind "orders 2024"
    at = slots[0]
    prefix, suffix = target[:at], target[at + 1 :]
    words = _words(text)
    span = len(words) - len(prefix) - len(suffix)
    if not 1 <= span <= MAX_SPAN:
        return None
    head, tail = words[: len(prefix)], words[len(words) - len(suffix) :]
    if [w.norm for w in head] != prefix or [w.norm for w in tail] != suffix:
        return None
    middle = words[len(prefix) : len(prefix) + span]
    if any(is_slot(w.norm) for w in middle):
        return None
    value = text[middle[0].start : middle[-1].end]
    if len(value) > 200:
        return None
    return target[at][1:-1], value


def resolve(store: GraphStore, ns: str, site: str, goal_text: str, params: Mapping[str, str]) -> GoalMatch:
    """Resolve `goal_text` to the transitions that achieve it, by the tiers above.

    Tiers past exact need `list_goals` (every store adapter has it); without it only exact
    keys match."""
    key = goal_key(goal_text, params)
    get_targets = store.goal_targets
    exact = get_targets(ns, goal_id(ns, site, key))
    if exact:
        return GoalMatch(targets=exact, match="exact", params=dict(params), automatic=True)
    listing: Callable[[str, str], tuple[Goal, ...]] | None = getattr(store, "list_goals", None)
    if listing is None:
        return _UNKNOWN
    known = [
        (goal, targets)
        for goal in visible_goals(listing(ns, site))
        if goal.text != key and (targets := get_targets(ns, goal.id))
    ]
    if not known:
        return _UNKNOWN

    def is_read(stored: str) -> tuple[int, ...]:
        return get_targets(ns, alias_goal(ns, site, same_words_key(stored), "read").id)

    # Tier 2: same words.
    words = same_words_key(key)
    same = [goal for goal, _ in known if same_words_key(goal.text) == words]
    if same and words:
        read = is_read(same[0].text)
        if read and _strong(words):
            return GoalMatch(
                targets=read, match="same_words", goal=same[0].text, params=dict(params), automatic=True
            )
        return GoalMatch(match="similar", goal=same[0].text, params=dict(params))

    # One-slot alignment: the caller gave no params but named the value in the text.
    if not params:
        bound = [(goal, targets, found) for goal, targets in known if (found := _align(goal_text, goal.text))]
        bindings = {found for _, _, found in bound}
        if len(bindings) == 1:
            ((slot, value),) = bindings
            reads = [(g, t) for g, t, _ in bound if is_read(g.text)]
            goal, targets = reads[0] if reads else (bound[0][0], bound[0][1])
            inferred = {slot: value}
            if reads:
                return GoalMatch(
                    targets=targets,
                    match="same_words",
                    goal=goal.text,
                    params=inferred,
                    inferred=True,
                    automatic=True,
                )
            return GoalMatch(match="similar", goal=goal.text, params=inferred, inferred=True)

    # Tier 3: similar, offered only.
    mine = frozenset(tokens(key))
    my_slots = frozenset(t for t in mine if is_slot(t))
    best: dict[frozenset[int], tuple[float, Goal]] = {}
    for goal, targets in known:
        theirs = frozenset(tokens(goal.text))
        if frozenset(t for t in theirs if is_slot(t)) != my_slots:
            continue
        score = _jaccard(mine, theirs)
        group = frozenset(targets)  # wordings that reach the same place are one candidate
        if group not in best or score > best[group][0]:
            best[group] = (score, goal)
    ranked = sorted(best.values(), key=lambda row: (-row[0], row[1].id))
    if ranked and ranked[0][0] >= MIN_SIMILAR:
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if ranked[0][0] - runner_up >= MIN_LEAD:
            return GoalMatch(match="similar", goal=ranked[0][1].text, params=dict(params))
    return _UNKNOWN
