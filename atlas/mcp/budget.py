"""Response token budget (R-MCP-2): cut lists from the end, then free text, never scalars."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from atlas.contracts.mcp_models import MCP_RESPONSE_TOKEN_BUDGET, response_tokens

# Lists are sacrificed in this order (least valuable first). Free text is first shortened to
# `_TEXT_FLOOR` characters, and only emptied once every list is gone.
_DROP_ORDER = ("entities_resolved", "choices", "skills", "route", "changes", "sites")
_FREE_TEXT = ("result_summary", "message")
_TEXT_FLOOR = 80


def _cut_text(result: BaseModel) -> tuple[BaseModel, bool]:
    """Shorten the longest free-text field above the floor to the floor."""
    longest = max(_FREE_TEXT, key=lambda name: len(getattr(result, name, "") or ""))
    text: str = getattr(result, longest, "") or ""
    if len(text) <= _TEXT_FLOOR:
        return result, False
    return result.model_copy(update={longest: text[:_TEXT_FLOOR]}), True


def _drop_item(result: BaseModel) -> tuple[BaseModel, bool]:
    for name in _DROP_ORDER:
        items: tuple[Any, ...] | None = getattr(result, name, None)
        if items:
            return result.model_copy(update={name: items[:-1]}), True
    return result, False


def _halve_text(result: BaseModel) -> tuple[BaseModel, bool]:
    for name in _FREE_TEXT:
        text: str = getattr(result, name, "") or ""
        if text:
            return result.model_copy(update={name: text[: len(text) // 2]}), True
    return result, False


def enforce_budget[T: BaseModel](result: T) -> T:
    """`result` itself when it fits `MCP_RESPONSE_TOKEN_BUDGET`; otherwise a copy with
    `truncated=True`, trailing list items dropped and free text shortened until it fits.
    Terminates: every pass removes at least one character or item, or stops.
    """
    if response_tokens(result) <= MCP_RESPONSE_TOKEN_BUDGET:
        return result
    cut: BaseModel = result.model_copy(update={"truncated": True})
    while response_tokens(cut) > MCP_RESPONSE_TOKEN_BUDGET:
        for step in (_cut_text, _drop_item, _halve_text):
            cut, changed = step(cut)
            if changed:
                break
        else:
            break
    return cut  # type: ignore[return-value]
