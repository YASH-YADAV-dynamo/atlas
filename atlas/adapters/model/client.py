"""ModelClient on the Anthropic SDK: one forced tool call per exploration step."""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import ValidationError

from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.types import ModelDecision, Params, Snapshot

TOOL_NAME = "next_step"
MAX_ELEMENTS = 120
MAX_HISTORY = 6

SYSTEM = (
    "You drive a web browser toward a goal, one step at a time. Reply only by calling the tool. "
    "A step is all the form fills needed on the current page plus exactly one trigger (click, or a "
    "fill/select/press on a single control). Controls are identified by role and exact accessible name, "
    "taken from the element list. When a list repeats a control name (elements marked in_list), set "
    "locator.row to text that appears only in the intended row. Refer to parameters through the `param` "
    "field using the slot name; a value shown as {slot} is hidden and can only be used through `param`. "
    "If the goal is already achieved on the current page, set done=true with action=null, and put the "
    "requested value in `answer` when the goal asks to find something."
)


def decision_schema() -> dict[str, Any]:
    """JSON schema of the tool input: ModelDecision without the token counters we fill ourselves."""
    schema = ModelDecision.model_json_schema()
    for field in ("input_tokens", "output_tokens"):
        schema["properties"].pop(field, None)
    schema["required"] = [name for name in schema.get("required", []) if name in schema["properties"]]
    return schema


def build_prompt(goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]) -> str:
    elements = "\n".join(
        f"- {e.role} | {e.name} | in_list={str(e.in_list).lower()}" for e in snapshot.elements[:MAX_ELEMENTS]
    )
    names = "\n".join(f"- {slot}: {value}" for slot, value in params.items()) or "(none)"
    past = "\n".join(f"- {line}" for line in history[-MAX_HISTORY:]) or "(none)"
    return (
        f"Goal: {goal_text}\n\nParameters:\n{names}\n\nPage: {snapshot.title} ({snapshot.url})\n\n"
        f"Interactive elements (role | name | in_list):\n{elements}\n\nVisible text:\n{snapshot.text}\n\n"
        f"Recent steps:\n{past}\n\nChoose the next step."
    )


class AnthropicModelClient:
    def __init__(self, api_key: str, model_id: str, max_tokens: int = 1024) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._tool: Any = {
            "name": TOOL_NAME,
            "description": "Report the next browser step, or that the goal is done.",
            "input_schema": decision_schema(),
        }

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        try:
            response = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._max_tokens,
                system=SYSTEM,
                messages=[{"role": "user", "content": build_prompt(goal_text, params, snapshot, history)}],
                tools=[self._tool],
                tool_choice={"type": "tool", "name": TOOL_NAME},
            )
        except anthropic.APIError as exc:
            raise ModelUnavailableError(f"model call failed: {type(exc).__name__}") from exc
        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None or not isinstance(block.input, dict):
            raise ModelUnavailableError("model returned no tool call")
        try:
            return ModelDecision.model_validate(
                {
                    **block.input,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                }
            )
        except ValidationError as exc:
            raise ModelUnavailableError("model returned an invalid decision") from exc
