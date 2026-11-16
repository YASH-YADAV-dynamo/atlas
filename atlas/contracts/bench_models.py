"""Benchmark contract (SPEC 6): what one task run records, how runs are summarised, and the
small types of the MCP comparison loop (arms M0 / M1). Charts are drawn only from these."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import Field, JsonValue

from atlas.contracts.types import Frozen, TaskSpec

Count = Annotated[int, Field(ge=0)]
RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
TemplateId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Arm(StrEnum):
    B0 = "b0"  # plain model-driven agent, no memory
    B1 = "b1"  # trajectory cache keyed by task template (the plain-store baseline)
    ATLAS = "atlas"
    M0 = "m0"  # MCP client with browser tools only
    M1 = "m1"  # the same plus the Atlas MCP server in runner mode


class Split(StrEnum):
    STREAM = "stream"  # experience stream, learning on
    HELDOUT = "heldout"  # held-out compositions, learning frozen
    MUTATION = "mutation"


class BenchTask(Frozen):
    """A task as the arms see it. Success is checked outside the arm, against the app's data."""

    template_id: TemplateId
    spec: TaskSpec
    needs_answer: bool = False


class TaskRecord(Frozen):
    """One task run by one arm: one JSON line in results/. No page text, prompts or secrets.

    `model_calls`/`input_tokens`/`output_tokens` come from the provider's usage fields.
    `tool_calls` (round trips) and `context_tokens` (tool definitions plus tool results,
    counted with `mcp_models.count_tokens`) are filled by arms M0 / M1 and are 0 elsewhere.
    """

    run_id: RunId
    arm: Arm
    seed: Count
    iteration: Count
    split: Split
    template_id: TemplateId
    success: bool
    model_calls: Count
    input_tokens: Count
    output_tokens: Count
    tool_calls: Count = 0
    context_tokens: Count = 0
    actions: Count
    replans: Count = 0
    wall_ms: Count


class SummaryRow(Frozen):
    """Aggregate of the records sharing (arm, split, iteration). Ratios are None when undefined.

    Percentiles are nearest-rank: the value at position ceil(p * n) in the sorted list.
    """

    arm: Arm
    split: Split
    iteration: Count
    tasks: Annotated[int, Field(ge=1)]
    successes: Count
    success_rate: Annotated[float, Field(ge=0, le=1)]
    model_calls_per_success: float | None  # model calls of successful tasks / successes
    tokens_per_success: float | None  # input + output tokens of successful tasks / successes
    zero_model_call_share: Annotated[float, Field(ge=0, le=1)]  # successes with 0 model calls / tasks
    tool_calls_per_task: float
    context_tokens_per_task: float
    wall_ms_p50: Count
    wall_ms_p95: Count


# ---- the MCP comparison loop (arms M0 / M1) ---------------------------------------------


class ToolDef(Frozen):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")]
    description: Annotated[str, Field(max_length=400)]
    input_schema: dict[str, JsonValue]


class ToolCall(Frozen):
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolTurn(Frozen):
    """One model turn: tool calls to execute, or none when the model is finished (`text`)."""

    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""
    input_tokens: Count = 0
    output_tokens: Count = 0


class ToolResult(Frozen):
    name: str
    text: str


class ToolModel(Protocol):
    """The model behind the MCP client. `history` alternates its turns and the tool results."""

    def turn(
        self, task_text: str, tools: tuple[ToolDef, ...], history: tuple[ToolTurn | ToolResult, ...]
    ) -> ToolTurn: ...


class LoopStats(Frozen):
    """What one task cost through the tool loop.

    `tool_calls` counts executed tool invocations (round trips). `context_tokens` =
    count_tokens of every tool definition offered (name + description + compact schema,
    once) plus count_tokens of every tool result text returned to the model.
    """

    finished: bool  # the model stopped calling tools before max_turns
    answer: str
    model_calls: Count
    tool_calls: Count
    context_tokens: Count
    input_tokens: Count
    output_tokens: Count
