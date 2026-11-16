"""Replay script generator (SHIP_PLAN 3.1): a planned route as one Playwright function.

The script runs the first `k` steps of a route with every slot bound from the caller's own
params, stops at the first failing step, and returns how many steps it finished. Every string
goes through `json.dumps`, so a hostile locator name or value can only ever be a string literal.
The script is whole or absent: it never ends mid-statement.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from atlas.contracts.errors import MissingParamError
from atlas.contracts.types import Action, ActionKind, Fill, Locator, RouteStep
from atlas.core.params import bind_action

MAX_SCRIPT_CHARS = 900

_HEAD = (
    "async (page) => { let n = 0; "
    'const r = (role, name, row) => (row ? page.locator("tr,li,[role=row],[role=listitem]")'
    ".filter({hasText: row}) : page).getByRole(role, {name, exact: true}).first(); "
    "try { "
)
_TAIL = "} catch (e) {} return {done: n, url: page.url(), title: await page.title()}; }"


@dataclass(frozen=True)
class Script:
    text: str
    steps: int  # how many route steps it covers


def _lit(value: str) -> str:
    """A JavaScript string literal: JSON, with the two line separators escaped as well."""
    text = json.dumps(value, ensure_ascii=False)
    for sep in (chr(0x2028), chr(0x2029)):
        text = text.replace(sep, chr(92) + "u" + format(ord(sep), "04x"))
    return text


def _target(locator: Locator) -> str:
    row = f", {_lit(locator.row)}" if locator.row is not None else ""
    return f"r({_lit(locator.role)}, {_lit(locator.name)}{row})"


def _fill(fill: Fill) -> str:
    value = fill.value or ""
    target = _target(fill.locator)
    if fill.kind is ActionKind.SELECT:
        return f"await {target}.selectOption({_lit(value)}); "
    if fill.kind is ActionKind.CHECK:
        return f"await {target}.{'uncheck' if value == 'false' else 'check'}(); "
    return f"await {target}.fill({_lit(value)}); "


def _trigger(action: Action) -> str:
    kind, value = action.kind, action.value or ""
    if kind is ActionKind.GOTO:
        return f"await page.goto(new URL({_lit(value)}, page.url()).href); "
    if action.locator is None:
        raise ValueError("a non-goto action needs a locator")
    target = _target(action.locator)
    if kind is ActionKind.FILL:
        return f"await {target}.fill({_lit(value)}); "
    if kind is ActionKind.SELECT:
        return f"await {target}.selectOption({_lit(value)}); "
    if kind is ActionKind.CHECK:
        return f"await {target}.{'uncheck' if value == 'false' else 'check'}(); "
    if kind is ActionKind.PRESS:
        return f"await {target}.press({_lit(value)}); "
    return f"await {target}.click(); "


def step_code(step: RouteStep, params: Mapping[str, str], index: int) -> str:
    """Statements for route step number `index` (1-based): its fills, its trigger, then the
    page settling and the done counter. Raises `MissingParamError` for an unbound slot."""
    action = bind_action(step.action, params)
    body = "".join(_fill(f) for f in action.fills) + _trigger(action)
    return f"{body}await page.waitForLoadState(); n = {index}; "


def build_script(
    route: Sequence[RouteStep],
    params: Mapping[str, str],
    *,
    max_chars: int = MAX_SCRIPT_CHARS,
    fits: Callable[[str], bool] | None = None,
) -> Script | None:
    """The longest whole prefix of `route` that fits `max_chars` (and `fits`, the caller's own
    size check, e.g. the response token budget); None when not even the first step fits or a
    slot is unbound."""
    best: Script | None = None
    body = ""
    for index, step in enumerate(route, start=1):
        try:
            body += step_code(step, params, index)
        except (MissingParamError, ValueError):
            break
        text = _HEAD + body + _TAIL
        if len(text) > max_chars or (fits is not None and not fits(text)):
            break
        best = Script(text, index)
    return best
