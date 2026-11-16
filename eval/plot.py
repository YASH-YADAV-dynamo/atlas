"""`python -m eval.plot [results/<run-id>]`: draw every chart from the raw records of a run.

Reads only `records.jsonl`; mean over seeds with a min/max band when there are several.
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Callable
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes

from atlas.contracts.bench_models import Arm, Split, SummaryRow, TaskRecord
from eval.budget import is_partial
from eval.results import read_records, summarise

COLORS = {Arm.B0: "#7a7a7a", Arm.B1: "#d98c1f", Arm.ATLAS: "#1f6fd9", Arm.M0: "#a35bd6", Arm.M1: "#1fa37a"}
LABELS = {
    Arm.B0: "B0 plain agent", Arm.B1: "B1 cached scripts", Arm.ATLAS: "A Atlas",
    Arm.M0: "M0 browser tools", Arm.M1: "M1 + Atlas MCP",
}  # fmt: skip

type Metric = Callable[[SummaryRow], float | None]


def per_seed(records: list[TaskRecord], arm: Arm, split: Split, metric: Metric) -> dict[int, list[float]]:
    """iteration -> the metric's value for each seed that has one."""
    out: dict[int, list[float]] = {}
    for seed in sorted({r.seed for r in records}):
        mine = [r for r in records if r.seed == seed and r.arm is arm and r.split is split]
        for row in summarise(mine):
            value = metric(row)
            if value is not None:
                out.setdefault(row.iteration, []).append(value)
    return out


def line_chart(
    ax: Axes, records: list[TaskRecord], split: Split, arms: tuple[Arm, ...], metric: Metric
) -> None:
    for arm in arms:
        series = per_seed(records, arm, split, metric)
        if not series:
            continue
        xs = sorted(series)
        means = [statistics.fmean(series[x]) for x in xs]
        ax.plot(xs, means, marker="o", color=COLORS[arm], label=LABELS[arm])
        if any(len(series[x]) > 1 for x in xs):
            ax.fill_between(
                xs, [min(series[x]) for x in xs], [max(series[x]) for x in xs], color=COLORS[arm], alpha=0.15
            )
    ax.set_xlabel("iteration (experience so far)")
    ax.grid(alpha=0.3)
    if ax.get_legend_handles_labels()[0]:
        ax.legend()


def save(fig: plt.Figure, path: Path) -> None:
    if is_partial(path.parent):
        fig.suptitle("partial run (stopped at the spending limit)", color="firebrick", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def draw_all(run_dir: Path) -> list[Path]:
    records = read_records(run_dir / "records.jsonl")
    core = (Arm.B0, Arm.B1, Arm.ATLAS)
    written: list[Path] = []

    fig, ax = plt.subplots(figsize=(6, 4))
    line_chart(ax, records, Split.HELDOUT, core, lambda r: r.success_rate)
    ax.set(ylabel="held-out success rate", ylim=(0, 1.05), title="Held-out success, learning frozen")
    written.append(run_dir / "heldout_success.png")
    save(fig, written[-1])

    for split in (Split.STREAM, Split.HELDOUT):
        fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4))
        line_chart(left, records, split, core, lambda r: r.model_calls_per_success)
        left.set(ylabel="model calls per successful task", title=f"Model calls ({split.value})")
        line_chart(right, records, split, core, lambda r: r.tokens_per_success)
        right.set(ylabel="input + output tokens per successful task", title=f"Tokens ({split.value})")
        written.append(run_dir / f"cost_{split.value}.png")
        save(fig, written[-1])

    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4))
    line_chart(left, records, Split.STREAM, (Arm.M0, Arm.M1), lambda r: r.tool_calls_per_task)
    left.set(ylabel="tool calls (round trips) per task", title="Arm M: round trips")
    line_chart(right, records, Split.STREAM, (Arm.M0, Arm.M1), lambda r: r.context_tokens_per_task)
    right.set(ylabel="context tokens per task (definitions + results)", title="Arm M: context tokens")
    written.append(run_dir / "mcp_round_trips_context.png")
    save(fig, written[-1])
    return written


def main() -> int:
    run_dir = Path(sys.argv[1])
    for path in draw_all(run_dir):
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
