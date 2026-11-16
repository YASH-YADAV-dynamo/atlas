"""Round-trip and context-token counting for arms M0 / M1. Targets (lane B):
    eval.mcp_loop.run_tool_loop(model: ToolModel,
                                tools: Mapping[str, tuple[ToolDef, Callable[[dict[str, JsonValue]], str]]],
                                task_text: str, *, max_turns: int = 30) -> LoopStats
The loop offers the tool definitions, executes every tool call the model makes, feeds the
result text back, and stops when a turn has no tool calls or after `max_turns` turns.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("eval.mcp_loop", reason="benchmark wave: implemented by lane B")

from atlas.contracts.bench_models import ToolCall, ToolDef, ToolResult, ToolTurn
from atlas.contracts.mcp_models import count_tokens
from eval.mcp_loop import run_tool_loop

SNAPSHOT = ToolDef(
    name="browser_snapshot",
    description="Return the page controls.",
    input_schema={"type": "object", "properties": {}},
)
CLICK = ToolDef(
    name="browser_click",
    description="Click a control by name.",
    input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
PAGE_TEXT = "link Orders; link Settings; " * 40


class ScriptedToolModel:
    """Plays a fixed list of turns and records what the loop showed it."""

    def __init__(self, turns: list[ToolTurn]) -> None:
        self._turns = list(turns)
        self.seen_tools: list[tuple[str, ...]] = []
        self.histories: list[tuple] = []

    def turn(self, task_text, tools, history):
        self.seen_tools.append(tuple(tool.name for tool in tools))
        self.histories.append(history)
        return (
            self._turns.pop(0) if self._turns else ToolTurn(tool_calls=(ToolCall(name="browser_snapshot"),))
        )


def tools(log: list):
    def snapshot(arguments):
        log.append(("browser_snapshot", arguments))
        return PAGE_TEXT

    def click(arguments):
        log.append(("browser_click", arguments))
        return "ok"

    return {"browser_snapshot": (SNAPSHOT, snapshot), "browser_click": (CLICK, click)}


def definition_tokens(*definitions: ToolDef) -> int:
    return sum(
        count_tokens(d.name + d.description + json.dumps(d.input_schema, separators=(",", ":")))
        for d in definitions
    )


def test_round_trips_and_context_tokens_are_counted():
    """R-BENCH-M: tool calls are round trips; context tokens are definitions plus tool results."""
    log: list = []
    model = ScriptedToolModel(
        [
            ToolTurn(tool_calls=(ToolCall(name="browser_snapshot"),), input_tokens=500, output_tokens=20),
            ToolTurn(
                tool_calls=(
                    ToolCall(name="browser_click", arguments={"name": "Orders"}),
                    ToolCall(name="browser_snapshot"),
                ),
                input_tokens=900,
                output_tokens=30,
            ),
            ToolTurn(text="The order is refunded.", input_tokens=1300, output_tokens=10),
        ]
    )
    stats = run_tool_loop(model, tools(log), "Refund order 7", max_turns=10)

    assert log == [("browser_snapshot", {}), ("browser_click", {"name": "Orders"}), ("browser_snapshot", {})]
    assert (stats.finished, stats.answer) == (True, "The order is refunded.")
    assert (stats.model_calls, stats.tool_calls) == (3, 3)
    assert (stats.input_tokens, stats.output_tokens) == (2700, 60)
    expected = definition_tokens(SNAPSHOT, CLICK) + 2 * count_tokens(PAGE_TEXT) + count_tokens("ok")
    assert stats.context_tokens == expected
    assert all(set(names) == {"browser_snapshot", "browser_click"} for names in model.seen_tools)
    assert model.histories[0] == ()
    assert [type(item) for item in model.histories[2]] == [
        ToolTurn,
        ToolResult,
        ToolTurn,
        ToolResult,
        ToolResult,
    ]
    assert model.histories[2][1] == ToolResult(name="browser_snapshot", text=PAGE_TEXT)


def test_loop_stops_at_the_turn_cap():
    """R-BENCH-M: a model that never finishes is cut at max_turns and reported as unfinished."""
    log: list = []
    stats = run_tool_loop(ScriptedToolModel([]), tools(log), "Refund order 7", max_turns=4)
    assert (stats.finished, stats.model_calls, stats.tool_calls, len(log)) == (False, 4, 4, 4)
    assert stats.context_tokens == definition_tokens(SNAPSHOT, CLICK) + 4 * count_tokens(PAGE_TEXT)


def test_unknown_tool_is_answered_not_raised():
    """R-BENCH-M: a call to a tool that is not offered costs a round trip and returns an error text."""
    log: list = []
    model = ScriptedToolModel([ToolTurn(tool_calls=(ToolCall(name="browser_eval"),)), ToolTurn(text="done")])
    stats = run_tool_loop(model, tools(log), "Refund order 7", max_turns=5)
    assert (stats.finished, stats.tool_calls, log) == (True, 1, [])
    result = model.histories[1][1]
    assert (
        isinstance(result, ToolResult)
        and result.name == "browser_eval"
        and "unknown tool" in result.text.lower()
    )
