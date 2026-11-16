"""The tool-calling loop behind arms M0 and M1, with round-trip and context-token accounting."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping

from pydantic import JsonValue

from atlas.contracts.bench_models import LoopStats, ToolDef, ToolModel, ToolResult, ToolTurn
from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.mcp_models import count_tokens

type ToolFn = Callable[[dict[str, JsonValue]], str]


def definition_tokens(definition: ToolDef) -> int:
    schema = json.dumps(definition.input_schema, separators=(",", ":"))
    return count_tokens(definition.name + definition.description + schema)


def run_tool_loop(
    model: ToolModel,
    tools: Mapping[str, tuple[ToolDef, ToolFn]],
    task_text: str,
    *,
    max_turns: int = 30,
) -> LoopStats:
    """Offer the tools, execute every call the model makes, feed the result texts back, and stop
    when a turn has no tool calls or after `max_turns` turns.

    `tool_calls` counts executed invocations (a call to an unoffered tool costs one and is answered
    with an error text). `context_tokens` is every definition once plus every result text.
    """
    definitions = tuple(definition for definition, _ in tools.values())
    history: list[ToolTurn | ToolResult] = []
    context = sum(definition_tokens(d) for d in definitions)
    model_calls = tool_calls = input_tokens = output_tokens = 0
    answer, finished = "", False
    for _ in range(max_turns):
        try:
            turn = model.turn(task_text, definitions, tuple(history))
        except ModelUnavailableError as exc:  # the task ends unsuccessfully; the run goes on
            sys.stderr.write(f"eval: model error in the tool loop: {exc}\n")
            break
        model_calls += 1
        input_tokens += turn.input_tokens
        output_tokens += turn.output_tokens
        history.append(turn)
        if not turn.tool_calls:
            answer, finished = turn.text, True
            break
        for call in turn.tool_calls:
            tool_calls += 1
            entry = tools.get(call.name)
            text = f"Error: unknown tool {call.name}" if entry is None else entry[1](call.arguments)
            context += count_tokens(text)
            history.append(ToolResult(name=call.name, text=text))
    return LoopStats(
        finished=finished,
        answer=answer,
        model_calls=model_calls,
        tool_calls=tool_calls,
        context_tokens=context,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
