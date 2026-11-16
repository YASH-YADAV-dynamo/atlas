"""Task parameters: routes are stored with `{slot}` placeholders and bound at plan time."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

from atlas.contracts.errors import MissingParamError
from atlas.contracts.types import Action, ActionKind, Fill, Locator

_SLOT_PATTERN = r"\{[a-z][a-z0-9_]*\}"
_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SPACES = re.compile(r"\s+")


def _value_to_slot(params: Mapping[str, str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for slot in sorted(params):
        if params[slot]:
            mapping.setdefault(params[slot], slot)
    return mapping


def _replace_values(text: str, mapping: Mapping[str, str], *, ignore_case: bool = False) -> str:
    """Replace whole-word occurrences of the values (longest first) with `{slot}`.

    Existing `{slot}` tokens match first and map to themselves, which keeps this idempotent.
    """
    if not mapping:
        return text
    fold = str.lower if ignore_case else str
    lookup = {fold(value): slot for value, slot in mapping.items()}
    values = sorted(mapping, key=len, reverse=True)
    body = "|".join(rf"(?<!\w){re.escape(value)}(?!\w)" for value in values)
    pattern = re.compile(f"({_SLOT_PATTERN})|{body}", re.IGNORECASE if ignore_case else 0)

    def swap(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return "{" + lookup[fold(match.group(0))] + "}"

    return pattern.sub(swap, text)


def goal_key(goal_text: str, params: Mapping[str, str]) -> str:
    templated = _replace_values(goal_text, _value_to_slot(params), ignore_case=True)
    return _SPACES.sub(" ", templated.lower()).strip()


def _map_locator(locator: Locator, convert: Callable[[str], str]) -> Locator:
    row = convert(locator.row) if locator.row is not None else None
    return locator.model_copy(update={"name": convert(locator.name), "row": row})


def templatise_action(action: Action, params: Mapping[str, str]) -> Action:
    """Replace parameter values with their slots.

    A value equal to a parameter (ignoring case) becomes `param`; a value that contains one
    keeps the rest literal around a `{slot}` ("hrithik roshan awards" -> "{person} awards").
    Locator names get `{slot}` wherever a value appears, ignoring case (SHIP_PLAN 5).
    """
    mapping = _value_to_slot(params)
    folded: dict[str, str] = {}
    for value, slot in mapping.items():
        folded.setdefault(value.lower(), slot)

    def names(text: str) -> str:
        return _replace_values(text, mapping, ignore_case=True)

    def literal(value: str | None) -> dict[str, object]:
        if value is None:
            return {}
        if value in mapping:
            return {"param": mapping[value], "value": None}
        if value.lower() in folded:
            return {"param": folded[value.lower()], "value": None}
        templated = names(value)
        return {"value": templated} if templated != value else {}

    fills: list[Fill] = []
    for fill in action.fills:
        update: dict[str, object] = {"locator": _map_locator(fill.locator, names)}
        update.update(literal(fill.value))
        fills.append(fill.model_copy(update=update))
    locator = _map_locator(action.locator, names) if action.locator is not None else None
    update = {"locator": locator, "fills": tuple(fills)}
    if action.kind is not ActionKind.GOTO:  # a goto path is identity, handled by url templates
        update.update(literal(action.value))
    return action.model_copy(update=update)


def _slots_in(text: str | None) -> set[str]:
    return set(_SLOT.findall(text)) if text else set()


def required_slots(action: Action) -> frozenset[str]:
    slots: set[str] = set()
    locators = [fill.locator for fill in action.fills]
    if action.locator is not None:
        locators.append(action.locator)
    for locator in locators:
        slots |= _slots_in(locator.name) | _slots_in(locator.row)
    slots.update(fill.param for fill in action.fills if fill.param is not None)
    for fill in action.fills:
        slots |= _slots_in(fill.value)
    if action.param is not None:
        slots.add(action.param)
    if action.kind is not ActionKind.GOTO:
        slots |= _slots_in(action.value)
    return frozenset(slots)


def bind_action(action: Action, params: Mapping[str, str]) -> Action:
    """Inverse of `templatise_action`; raises `MissingParamError` for an unfilled slot."""

    def lookup(slot: str) -> str:
        if slot not in params:
            raise MissingParamError(f"missing parameter: {slot}")
        return params[slot]

    def names(text: str) -> str:
        return _SLOT.sub(lambda match: lookup(match.group(1)), text)

    fills: list[Fill] = []
    for fill in action.fills:
        update: dict[str, object] = {"locator": _map_locator(fill.locator, names)}
        if fill.param is not None:
            update.update(param=None, value=lookup(fill.param))
        elif fill.value is not None:
            update.update(value=names(fill.value))
        fills.append(fill.model_copy(update=update))
    locator = _map_locator(action.locator, names) if action.locator is not None else None
    update = {"locator": locator, "fills": tuple(fills)}
    if action.param is not None:
        update.update(param=None, value=lookup(action.param))
    elif action.value is not None and action.kind is not ActionKind.GOTO:
        update.update(value=names(action.value))
    return action.model_copy(update=update)
