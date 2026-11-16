"""Lenient MCP input (SHIP_PLAN D13.4): fix the mistakes that have one clear meaning, say so.

Only the MCP layer runs this, before validation; the contract models stay strict (`extra="forbid"`),
so REST and the SDK are unchanged. `coerce(tool, arguments)` returns the corrected arguments and
short notes ("dropped locator.in_list"). It never raises, never invents a top-level key, and never
guesses a goal: what cannot be fixed with one meaning is left alone and fails validation.
"""

from __future__ import annotations

from typing import Any, cast

from atlas.contracts.mcp_models import MAX_NOTES, NOTE_LENGTH, TOOLS

type Obj = dict[str, Any]

_LOCATOR_KEYS = frozenset({"role", "name", "row"})
_ACTION_KEYS = frozenset({"kind", "locator", "param", "value", "fills"})
_FILL_KEYS = frozenset({"locator", "kind", "param", "value"})
_STEP_KEYS = frozenset({"action", "to", "ok", "ms"})
# Identity-like keys are refused, never silently dropped: no input may name a namespace.
_REFUSED = frozenset({"ns", "namespace", "tenant", "tenant_id"})
_FIELDS: dict[str, frozenset[str]] = {tool.name: frozenset(tool.input_model.model_fields) for tool in TOOLS}


class _Notes:
    def __init__(self) -> None:
        self._items: list[str] = []

    def add(self, text: str) -> None:
        text = text[:NOTE_LENGTH]
        if text not in self._items:
            self._items.append(text)

    def result(self) -> tuple[str, ...]:
        return tuple(self._items[:MAX_NOTES])


def _is_obj(value: object) -> bool:
    return isinstance(value, dict)


def _locator(raw: object, where: str, notes: _Notes) -> object:
    if not _is_obj(raw):
        return raw
    loc = cast(Obj, raw)
    for key in loc:
        if key not in _LOCATOR_KEYS:
            notes.add(f"dropped {where}.{key}")
    return {k: v for k, v in loc.items() if k in _LOCATOR_KEYS}


def _one_source(item: Obj, params: Obj, where: str, notes: _Notes) -> None:
    """`param` and `value` both set: press keeps the key name; the rest keep a known task param."""
    if item.get("param") is None or item.get("value") is None:
        return
    if item.get("kind") == "press":
        item.pop("param")
        notes.add(f"{where}: press keeps value, param ignored")
    elif item["param"] in params:
        item.pop("value")
        notes.add(f"{where}: param and value set; kept param")
    else:
        item.pop("param")
        notes.add(f"{where}: param and value set; kept value")


def _fill(raw: object, params: Obj, where: str, notes: _Notes) -> object:
    if not _is_obj(raw):
        return raw
    fill = dict(cast(Obj, raw))
    for key in [k for k in fill if k not in _FILL_KEYS]:
        notes.add(f"dropped {where}.{key}")
        del fill[key]
    if "locator" in fill:
        fill["locator"] = _locator(fill["locator"], "locator", notes)
    _one_source(fill, params, where, notes)
    return fill


def _action(raw: object, params: Obj, where: str, notes: _Notes) -> object:
    if not _is_obj(raw):
        return raw
    action = dict(cast(Obj, raw))
    if action.get("kind") == "goto":
        for alias in ("url", "path"):  # a goto address under another name is its value
            if alias in action:
                address = action.pop(alias)
                if action.get("value") is None:
                    action["value"] = address
                    notes.add(f"{where}: {alias} taken as value")
        if action.get("locator") is not None:
            del action["locator"]
            notes.add("goto: locator ignored")
    for key in [k for k in action if k not in _ACTION_KEYS]:
        notes.add(f"dropped {where}.{key}")
        del action[key]
    if "locator" in action:
        action["locator"] = _locator(action["locator"], "locator", notes)
    _one_source(action, params, where, notes)
    fills = action.get("fills")
    if isinstance(fills, list):
        action["fills"] = [_fill(f, params, "fill", notes) for f in cast(list[object], fills)]
    return action


def _step(raw: object, params: Obj, notes: _Notes) -> object:
    if not _is_obj(raw):
        return raw
    step = dict(cast(Obj, raw))
    for key in [k for k in step if k not in _STEP_KEYS]:
        notes.add(f"dropped step.{key}")
        del step[key]
    if "action" in step:
        step["action"] = _action(step["action"], params, "step", notes)
    return step


def coerce(tool: str, arguments: object) -> tuple[Any, tuple[str, ...]]:
    """The arguments with their one-meaning mistakes fixed, and a note per correction (at most 3)."""
    notes = _Notes()
    try:
        if not isinstance(arguments, dict):
            return arguments, ()
        args = dict(cast(Obj, arguments))
        allowed = _FIELDS.get(tool)
        if allowed is not None:
            for key in [k for k in args if k not in allowed and k not in _REFUSED]:
                notes.add(f"dropped {key}")
                del args[key]
        raw_params = args.get("params")
        params: Obj = cast(Obj, raw_params) if isinstance(raw_params, dict) else {}
        if "action" in args:
            args["action"] = _action(args["action"], params, "action", notes)
        steps = args.get("steps")
        if isinstance(steps, list):
            args["steps"] = [_step(s, params, notes) for s in cast(list[object], steps)]
        if isinstance(args.get("done"), bool):
            del args["done"]
            notes.add("dropped done (a count is expected)")
        return args, notes.result()
    except Exception:
        return arguments, ()
