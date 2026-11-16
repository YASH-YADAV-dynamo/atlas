"""Benchmark results: JSON-lines storage of task records and the aggregation charts are drawn from."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from pathlib import Path

from atlas.contracts.bench_models import SummaryRow, TaskRecord


def append_records(path: Path, records: Iterable[TaskRecord]) -> None:
    """Append one JSON object per line; the file and its folder are created on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")


def read_records(path: Path) -> list[TaskRecord]:
    with path.open(encoding="utf-8") as handle:
        return [TaskRecord.model_validate_json(line) for line in handle if line.strip()]


def _nearest_rank(sorted_values: Sequence[int], p: float) -> int:
    return sorted_values[math.ceil(p * len(sorted_values)) - 1]


def _row(group: Sequence[TaskRecord]) -> SummaryRow:
    first = group[0]
    wins = [item for item in group if item.success]
    times = sorted(item.wall_ms for item in group)
    return SummaryRow(
        arm=first.arm,
        split=first.split,
        iteration=first.iteration,
        tasks=len(group),
        successes=len(wins),
        success_rate=len(wins) / len(group),
        model_calls_per_success=sum(w.model_calls for w in wins) / len(wins) if wins else None,
        tokens_per_success=(
            sum(w.input_tokens + w.output_tokens for w in wins) / len(wins) if wins else None
        ),
        zero_model_call_share=sum(w.model_calls == 0 for w in wins) / len(group),
        tool_calls_per_task=sum(item.tool_calls for item in group) / len(group),
        context_tokens_per_task=sum(item.context_tokens for item in group) / len(group),
        wall_ms_p50=_nearest_rank(times, 0.5),
        wall_ms_p95=_nearest_rank(times, 0.95),
    )


def summarise(records: Sequence[TaskRecord]) -> list[SummaryRow]:
    """One row per (arm, split, iteration), sorted by those values. Every number comes from `records`."""
    groups: dict[tuple[str, str, int], list[TaskRecord]] = {}
    for item in records:
        groups.setdefault((item.arm.value, item.split.value, item.iteration), []).append(item)
    return [_row(groups[key]) for key in sorted(groups)]
