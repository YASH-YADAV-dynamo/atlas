"""ModelClient on any OpenAI-compatible chat-completions endpoint (DeepSeek, OpenAI, vLLM...), over httpx."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from atlas.adapters.model.client import SYSTEM, TOOL_NAME, build_prompt, decision_schema
from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.types import ModelDecision, Params, Snapshot

DEFAULT_BASE_URL = "https://api.deepseek.com"


def chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def post_chat(client: httpx.Client, base_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST one chat-completions request; any failure becomes ModelUnavailableError (no secrets in it)."""
    try:
        response = client.post(
            chat_url(base_url), json=payload, headers={"Authorization": f"Bearer {api_key}"}
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        raise ModelUnavailableError(f"model call failed: HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelUnavailableError(f"model call failed: {type(exc).__name__}") from exc
    if not isinstance(body, dict):
        raise ModelUnavailableError("model returned an unreadable response")
    return body


def first_json_object(text: str) -> object:
    """The first JSON value in `text`. DeepSeek sometimes sends a complete argument object followed
    by more characters in one tool call; the complete object is the decision, the rest is ignored."""
    value, _ = json.JSONDecoder().raw_decode(text.lstrip())
    return value


def usage_of(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage") or {}
    return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


class OpenAICompatModelClient:
    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int = 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key, self._model_id = api_key, model_id
        self._base_url, self._max_tokens = base_url, max_tokens
        self._http = httpx.Client(timeout=60.0, transport=transport)
        self._tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": "Report the next browser step, or that the goal is done.",
                "parameters": decision_schema(),
            },
        }

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        payload = {
            "model": self._model_id,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(goal_text, params, snapshot, history)},
            ],
            "tools": [self._tool],
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        }
        if "deepseek" in self._base_url:
            # DeepSeek's default thinking mode rejects a forced tool_choice and bills reasoning tokens.
            payload["thinking"] = {"type": "disabled"}
        body = post_chat(self._http, self._base_url, self._api_key, payload)
        try:
            call = body["choices"][0]["message"]["tool_calls"][0]
            arguments = first_json_object(call["function"]["arguments"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelUnavailableError("model returned no tool call") from exc
        if not isinstance(arguments, dict):
            raise ModelUnavailableError("model returned no tool call")
        if "done" not in arguments:  # DeepSeek sometimes omits `done` next to an action
            arguments = {**arguments, "done": arguments.get("action") is None}
        tokens_in, tokens_out = usage_of(body)
        try:
            return ModelDecision.model_validate(
                {**arguments, "input_tokens": tokens_in, "output_tokens": tokens_out}
            )
        except ValidationError as exc:
            raise ModelUnavailableError("model returned an invalid decision") from exc
