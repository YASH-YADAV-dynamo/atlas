"""Model clients for the benchmark. Token counts are the provider's `usage` numbers, never estimates.

The exploration client (arms B0, B1, A) is the Atlas `ModelClient` port; the tool-calling client
(arms M0, M1) turns the Anthropic tool-use API into the `ToolModel` protocol.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx

from atlas.adapters.model.openai_compat import DEFAULT_BASE_URL, first_json_object, post_chat, usage_of
from atlas.contracts.bench_models import ToolCall, ToolDef, ToolResult, ToolTurn
from atlas.contracts.errors import ModelUnavailableError

SYSTEM = (
    "You operate a web back-office through tools. Complete the task, then reply with a short final "
    "answer and no tool call. Use as few tool calls as you can."
)


def _messages(history: tuple[ToolTurn | ToolResult, ...]) -> list[dict[str, Any]]:
    """Alternate assistant turns and user tool results; tool_use ids are positional."""
    messages: list[dict[str, Any]] = []
    pending: list[str] = []
    results: list[dict[str, Any]] = []
    for index, item in enumerate(history):
        if isinstance(item, ToolTurn):
            if results:
                messages.append({"role": "user", "content": results})
                results = []
            content: list[dict[str, Any]] = [{"type": "text", "text": item.text}] if item.text else []
            pending = [f"call_{index}_{n}" for n in range(len(item.tool_calls))]
            content += [
                {"type": "tool_use", "id": cid, "name": call.name, "input": call.arguments}
                for cid, call in zip(pending, item.tool_calls, strict=True)
            ]
            messages.append({"role": "assistant", "content": content})
        else:
            results.append({"type": "tool_result", "tool_use_id": pending.pop(0), "content": item.text})
    if results:
        messages.append({"role": "user", "content": results})
    return messages


class AnthropicToolModel:
    """`ToolModel` on the Anthropic tool-use API."""

    def __init__(self, api_key: str, model_id: str, max_tokens: int = 1024) -> None:
        self._client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=2)
        self._model_id, self._max_tokens = model_id, max_tokens

    def turn(
        self, task_text: str, tools: tuple[ToolDef, ...], history: tuple[ToolTurn | ToolResult, ...]
    ) -> ToolTurn:
        offered: Any = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
        ]
        messages: Any = [{"role": "user", "content": task_text}, *_messages(history)]
        try:
            response = self._client.messages.create(
                model=self._model_id,
                max_tokens=self._max_tokens,
                system=SYSTEM,
                messages=messages,
                tools=offered,
            )
        except anthropic.APIError as exc:
            raise ModelUnavailableError(f"model call failed: {type(exc).__name__}") from exc
        calls = tuple(
            ToolCall(name=b.name, arguments=b.input if isinstance(b.input, dict) else {})
            for b in response.content
            if b.type == "tool_use"
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return ToolTurn(
            tool_calls=calls,
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


def _openai_messages(task_text: str, history: tuple[ToolTurn | ToolResult, ...]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task_text},
    ]
    pending: list[str] = []
    for index, item in enumerate(history):
        if isinstance(item, ToolTurn):
            pending = [f"call_{index}_{n}" for n in range(len(item.tool_calls))]
            turn: dict[str, Any] = {"role": "assistant", "content": item.text or None}
            if pending:
                turn["tool_calls"] = [
                    {
                        "id": cid,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                    }
                    for cid, call in zip(pending, item.tool_calls, strict=True)
                ]
            messages.append(turn)
        else:
            messages.append({"role": "tool", "tool_call_id": pending.pop(0), "content": item.text})
    return messages


class OpenAIToolModel:
    """`ToolModel` on an OpenAI-compatible chat-completions endpoint (function calling)."""

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

    def turn(
        self, task_text: str, tools: tuple[ToolDef, ...], history: tuple[ToolTurn | ToolResult, ...]
    ) -> ToolTurn:
        payload: dict[str, Any] = {
            "model": self._model_id,
            "max_tokens": self._max_tokens,
            "messages": _openai_messages(task_text, history),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ],
        }
        if "deepseek" in self._base_url:
            # same model settings as the exploration client (arms B0, B1, A): thinking off
            payload["thinking"] = {"type": "disabled"}
        body = post_chat(self._http, self._base_url, self._api_key, payload)
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailableError("model returned no message") from exc
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            try:
                arguments = first_json_object(raw["function"]["arguments"] or "{}")
                name = raw["function"]["name"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ModelUnavailableError("model returned a malformed tool call") from exc
            calls.append(ToolCall(name=name, arguments=arguments if isinstance(arguments, dict) else {}))
        tokens_in, tokens_out = usage_of(body)
        return ToolTurn(
            tool_calls=tuple(calls),
            text=message.get("content") or "",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
