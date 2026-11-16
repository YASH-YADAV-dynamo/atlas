"""Benchmark budget (cost guard, lane G). Targets:

eval.budget.run_within_budget(items: Iterable[T], run: Callable[[T], TaskRecord],
                              spent_usd: Callable[[], float], max_usd: float | None)
    -> tuple[list[TaskRecord], bool]
    runs items in order; stops before the next item once spent_usd() >= max_usd, or when
    `run` raises BudgetExceededError; the bool says the result is partial.
eval.budget.write_meta(out_dir: Path, *, partial: bool, spent_usd: float, max_usd: float | None)
    writes out_dir / "run_meta.json"
eval.run.parse_args: `--max-usd`, and a smoke configuration sized to about $1.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("eval.budget", reason="cost guard: implemented by lane G")

from atlas.contracts.bench_models import Arm, Split, TaskRecord
from atlas.contracts.errors import BudgetExceededError
from eval.budget import run_within_budget, write_meta
from eval.run import parse_args


def record(index: int) -> TaskRecord:
    return TaskRecord(
        run_id="r", arm=Arm.B0, seed=1, iteration=index, split=Split.STREAM, template_id="refund_order",
        success=True, model_calls=1, input_tokens=1, output_tokens=1, actions=1, wall_ms=1,
    )  # fmt: skip


class Spender:
    """Each task costs 30 cents."""

    def __init__(self, broke_at: int | None = None) -> None:
        self.spent, self.ran, self.broke_at = 0.0, [], broke_at

    def run(self, index: int) -> TaskRecord:
        if self.broke_at is not None and index >= self.broke_at:
            raise BudgetExceededError("model budget exhausted")
        self.ran.append(index)
        self.spent += 0.30
        return record(index)


def test_benchmark_stops_at_the_limit_and_keeps_what_it_has():
    """R-COST-4: with --max-usd the run stops once the ledger reaches the limit; finished tasks
    are kept and the result is flagged partial."""
    spender = Spender()
    records, partial = run_within_budget(range(10), spender.run, lambda: spender.spent, 1.0)
    assert (spender.ran, partial) == ([0, 1, 2, 3], True)
    assert [r.iteration for r in records] == [0, 1, 2, 3]

    spender = Spender()
    records, partial = run_within_budget(range(3), spender.run, lambda: spender.spent, 1.0)
    assert (len(records), partial) == (3, False)
    records, partial = run_within_budget(range(5), Spender().run, lambda: 0.0, None)
    assert (len(records), partial) == (5, False)


def test_a_budget_error_mid_run_is_a_graceful_stop():
    """R-COST-4: the hard cap firing inside a task ends the benchmark without a traceback."""
    spender = Spender(broke_at=2)
    records, partial = run_within_budget(range(6), spender.run, lambda: spender.spent, None)
    assert ([r.iteration for r in records], partial) == ([0, 1], True)


def test_partial_runs_are_marked_on_disk(tmp_path):
    """R-COST-4: run_meta.json records that the numbers come from a partial run."""
    write_meta(tmp_path, partial=True, spent_usd=0.9731, max_usd=1.0)
    meta = json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))
    assert (meta["partial"], meta["max_usd"]) == (True, 1.0)
    assert meta["spent_usd"] == pytest.approx(0.9731)
    write_meta(tmp_path, partial=False, spent_usd=0.2, max_usd=None)
    assert json.loads((tmp_path / "run_meta.json").read_text(encoding="utf-8"))["partial"] is False


def test_smoke_configuration_is_sized_for_about_a_dollar(monkeypatch):
    """R-COST-4: the default (smoke) run is 1 seed, about 30 tasks per arm, 10 held-out tasks;
    --max-usd is parsed and has no default limit of its own."""
    monkeypatch.delenv("EVAL_FULL", raising=False)
    smoke = parse_args([])
    assert smoke.seeds == (1,)
    assert smoke.heldout == 10
    assert 20 <= smoke.iterations * smoke.stream_per_iteration <= 30
    assert smoke.max_usd is None
    assert parse_args(["--max-usd", "1.25"]).max_usd == 1.25
    assert len(parse_args(["--full"]).seeds) > 1
