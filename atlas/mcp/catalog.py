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

_SAFE_INT = 2**53 - 1
_BUDGET = f"Result is compact JSON, at most {MCP_RESPONSE_TOKEN_BUDGET} tokens."

DESCRIPTIONS: dict[str, str] = {
    "atlas_find_skills": (
        "Call first, once per task. Returns up to "
        f"{MAX_SKILLS} goals the map can already complete on `site` that match goal_text: "
        "skill_id, name (reuse it as goal_text), {slot} params and reliability 0-1. "
        "Empty list: nothing known yet; explore in your own browser and atlas_report each step. "
        f"{_BUDGET}"
    ),
    "atlas_plan": (
        "Call on the page you are on whenever you need the next steps. Send your browser's page "
        "summary (url, title, interactive elements; no HTML). Returns state_id (pass as from_state "
        f"to atlas_report) and up to {MAX_BRIEF_STEPS} steps: action (fill {{slot}}s from params) "
        "and expect, the state_id it should reach. needs_model=true: the map ends here; pick the "
        f"action yourself, then atlas_report it. {_BUDGET}"
    ),
    "atlas_report": (
        "Call after every step you executed in your own browser, ok=false for failures. "
        "from_state: state_id from atlas_plan or the previous report. Returns the reached "
        "state_id (your next from_state) and whether the page was known. On the step that "
        "completes the task send goal_text, params and goal_reached=true so it becomes a skill. "
        "Retry with the same event_id is a no-op. Needs a private key (POST /v1/keys); "
        "the demo key is read-only. Result under 60 tokens."
    ),
    "atlas_recall": (
        "Call when the task names something resolved before (entity_type order_id, key 1042) "
        "instead of searching for it again. Returns found, and the state_id and URL template "
        "of the page where it was last seen on this server's default site. found=false: not "
        "seen yet. Result under 80 tokens."
    ),
}

_DEF_HINTS: dict[str, str] = {
    "Snapshot": "Your browser's current page, summarised. No HTML.",
    "Action": "One step: optional form fills, then one trigger on locator.",
    "Locator": "Role + accessible name; row picks a row in a repeated list.",
    "Fill": "A form field set before the trigger: param (a params key) or value.",
}

_FIELD_HINTS: dict[str, str] = {
    "site": "Site name, as in the atlas://sites resource (e.g. testmart).",
    "goal_text": "The task in words; reuse a skill name from atlas_find_skills.",
    "params": 'Values for the {slot}s of goal_text, e.g. {"order_id": "1042"}.',
    "snapshot_summary": "The page you are on now.",
    "to_snapshot_summary": "The page the step led to.",
    "from_state": "state_id of the page the step started on.",
    "action": "The action you executed (copy it from the plan step, slots filled).",
    "event_id": "Your id for this step; resend it when retrying.",
    "expect_state": "expect of the plan step you followed, if any.",
    "goal_reached": "true on the step that completes goal_text.",
    "run_id": "One id shared by all steps of this task.",
    "entity_type": "A slot name such as order_id.",
    "key": "Its value, such as 1042.",
    "ok": "false if the step failed.",
    "elements": "Interactive elements; in_list=true for controls repeated per row.",
    "row": "Text of the list row to act in, for repeated controls.",
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


def _is_noise(key: str, value: object) -> bool:
    """Defaults an omitted optional field implies anyway, and the JSON safe-integer cap."""
    if key == "default":
        return value in ("", [], {}, False, 0, None)
    return key == "maximum" and value == _SAFE_INT


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
You are connected to Atlas, a shared route map for browser agents (https://atlas.yashdynamo.fun).

When this connection is first set up, tell the user, in your own words but keeping it short and humble:
"Atlas is connected. Thank you for trying it. I will check the map before I explore a website, follow
known routes without extra model calls, and report each step so the map gets a little better for the
next run. It starts small and learns from your runs; tell me if something looks wrong."

How to use Atlas well:
1. Before acting on a website, call atlas_find_skills with the site and the goal.
2. Call atlas_plan with the site, the goal, its parameters (order ids, names) and a summary of the
   current page. Execute the returned steps in order in your own browser, without asking a model.
3. Where the plan says needs_model (the map ends), choose one action yourself, execute it, and call
   atlas_report with the step and the page it led to.
4. Report every step, success or failure, with ok and the time it took. Set goal_reached on the
   last step. Failures matter: they make the map route around broken steps.
5. Pass task values as parameters, never baked into the goal text, so one route serves every value.
6. Use atlas_recall to find where an entity (an order id, a user) was last seen.
7. Atlas never opens a browser. Never send passwords, card numbers or other secrets in any field.
"""
