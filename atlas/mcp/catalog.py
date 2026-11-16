"""How the tools read to an agent in `tools/list`: descriptions and compact input schemas.

The contract (`atlas.contracts.mcp_models.TOOLS`) owns names, input models and validation;
this module only presents them. Each listed description says when to call the tool, what it
returns and what it costs; each input schema drops pydantic noise (titles, `null` branches,
model docstrings) and carries one short hint per field instead. Validation still runs on the
contract model, so a schema here can never accept what the contract refuses.

Planner mode (the hosted endpoint) is presented this way; runner mode, whose definitions have a
tighter budget, lists the contract descriptions and schemas unchanged.
"""

from __future__ import annotations

import json
from typing import Any, cast

from atlas.contracts.mcp_models import (
    MAX_BRIEF_STEPS,
    MAX_SKILLS,
    MCP_RESPONSE_TOKEN_BUDGET,
    Mode,
    ToolSpec,
    count_tokens,
    tools_for,
)

_BUDGET = f"Result is compact JSON, at most {MCP_RESPONSE_TOKEN_BUDGET} tokens."

DESCRIPTIONS: dict[str, str] = {
    "atlas_find_skills": (
        "Call only to browse what the map can do on `site` for goal_text; atlas_plan already matches "
        f"goals. Returns up to {MAX_SKILLS} goals: name (reuse as goal_text), {{slot}} params, "
        f"reliability 0-1. {_BUDGET}"
    ),
    "atlas_plan": (
        "Call first, once per task, from the page you are on (send url and title; elements only if "
        "one URL shows different screens). Returns state_id, plan_id and one of: open_url (read "
        "goal: navigate there), script (with replay=script: run it with Playwright, it returns "
        f"{{done}}), or up to {MAX_BRIEF_STEPS} route steps (fill {{slot}}s from params; expect is the "
        "state_id each reaches). needs_model=true: explore, then atlas_report. match/goal: the stored "
        f"wording that matched; reuse it. {_BUDGET}"
    ),
    "atlas_report": (
        "Call once per task, after it. Batch: from_state + steps [{action, to, ok}] (put fills in "
        "the action that submits them), goal_text, params, goal_reached, kind=read when reaching the "
        "page was the goal. Replay: plan_id + done + ok (optional after a clean exact replay). "
        "Single: from_state, action, to_snapshot_summary. Returns state_id, learned, goal. "
        "Retry with the same event_id is a no-op. Needs a private key (POST /v1/keys). "
        "Result under 60 tokens."
    ),
    "atlas_recall": (
        "Call when the task names something resolved before (entity_type order_id, key 1042) "
        "instead of searching for it again. Returns found, and the state_id and URL template "
        "of the page where it was last seen on this server's default site. found=false: not "
        "seen yet. Result under 80 tokens."
    ),
}

_DEF_HINTS: dict[str, str] = {
    "Snapshot": "Current page summary, no HTML.",
    "Action": "Fills, then one trigger on locator.",
    "Locator": "Role + accessible name; row: list row text.",
    "Fill": "Field set before the trigger: param or value.",
}

_FIELD_HINTS: dict[str, str] = {
    "site": "Site name, e.g. testmart.",
    "goal_text": "The task in words; reuse a skill name from atlas_find_skills.",
    "params": 'Values for goal_text {slot}s, e.g. {"order_id": "1042"}.',
    "snapshot_summary": "The page you are on now.",
    "to_snapshot_summary": "The page the step led to.",
    "from_state": "state_id of the page the step started on.",
    "action": "The action executed, slots filled.",
    "event_id": "Your id for this step; resend it when retrying.",
    "expect_state": "expect of the plan step you followed, if any.",
    "goal_reached": "true on the step that completes goal_text.",
    "run_id": "One id shared by all steps of this task.",
    "entity_type": "A slot name such as order_id.",
    "key": "Its value, such as 1042.",
    "ok": "false if the step failed.",
    "elements": "in_list=true for controls repeated per row.",
    "row": "Text of the list row.",
}


type Schema = dict[str, Any]


def _without_null(schema: Schema) -> Schema:
    """`anyOf [X, null]` with default null -> X: an optional field is simply omitted."""
    branches = schema.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2 and {"type": "null"} in branches:
        rest: Schema = dict(next(b for b in cast(list[Schema], branches) if b != {"type": "null"}))
        rest.update({k: v for k, v in schema.items() if k not in ("anyOf", "default")})
        return rest
    return schema


# Size and format limits are enforced by the contract model (the server truncates or refuses), so
# the listed schema leaves them out: they cost tokens in every agent's context for no new safety.
_LIMITS = frozenset({"maxLength", "minLength", "maxItems", "minItems", "minimum", "maximum", "pattern"})


def _is_noise(key: str, value: object) -> bool:
    """Defaults an omitted optional field implies anyway, and limits the server enforces itself."""
    if key == "default":
        return value in ("", [], {}, False, 0, None, "fill", "act")
    return key in _LIMITS


def _compact_value(value: object) -> object:
    if isinstance(value, list):
        return [_compact_value(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return _compact(cast(Schema, value))
    return value


def _compact(node: Schema, field: str | None = None) -> Schema:
    out: Schema = {}
    for key, value in _without_null(node).items():
        if key in ("title", "description") or _is_noise(key, value):
            continue
        if key == "properties" and isinstance(value, dict):
            props = cast(dict[str, Schema], value)
            out[key] = {name: _compact(prop, name) for name, prop in props.items()}
        elif key == "$defs" and isinstance(value, dict):
            defs = cast(dict[str, Schema], value)
            out[key] = {name: _with_hint(_compact(d), _DEF_HINTS.get(name)) for name, d in defs.items()}
        elif key == "patternProperties" and isinstance(value, dict):
            out["additionalProperties"] = {"type": "string"}  # a map of slot name to value
        else:
            out[key] = _compact_value(value)
    return _with_hint(out, _FIELD_HINTS.get(field) if field else None)


def _with_hint(schema: Schema, hint: str | None) -> Schema:
    return schema if hint is None else {"description": hint, **schema}


def _presented(spec: ToolSpec, mode: Mode) -> bool:
    return mode is Mode.PLANNER and spec.name in DESCRIPTIONS


def listed_description(spec: ToolSpec, mode: Mode) -> str:
    return DESCRIPTIONS[spec.name] if _presented(spec, mode) else spec.description


def listed_schema(spec: ToolSpec, mode: Mode) -> dict[str, Any]:
    if not _presented(spec, mode):
        return spec.input_model.model_json_schema()
    return _compact(spec.input_model.model_json_schema())


def listed_tokens(spec: ToolSpec, mode: Mode) -> int:
    """Context cost of one listed definition, counted like `definition_tokens`."""
    schema = json.dumps(listed_schema(spec, mode), separators=(",", ":"))
    return count_tokens(spec.name + listed_description(spec, mode) + schema)


def listed_total(mode: Mode) -> int:
    return sum(listed_tokens(spec, mode) for spec in tools_for(mode))


SERVER_INSTRUCTIONS = """\
Atlas is a shared route map for browser agents (https://atlas.yashdynamo.fun). On first connect tell
the user briefly: "Atlas is connected. I will check the map before exploring a site, replay known
routes without extra model calls, and report back so it learns. Tell me if something looks wrong."

1. Call atlas_plan with site, goal_text, params and the current page (url, title). Same goal wording
   every run, task values in params ({slot}s in the goal).
2. open_url: go there. script: run it with Playwright. route: run the steps. Clean exact replay:
   atlas_report is optional. needs_model: explore yourself.
3. After exploring, call atlas_report once: steps in order, goal_reached, kind=read for lookups.
   After a replay that failed or ended elsewhere, report plan_id and done.
4. Never send passwords, card numbers or other secrets in any field.
"""
