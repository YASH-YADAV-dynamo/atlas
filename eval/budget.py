"""Benchmark spending limit: stop cleanly at --max-usd or when the hard cap fires (R-COST-4)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

from atlas.contracts.bench_models import TaskRecord
from atlas.contracts.errors import BudgetExceededError

META_FILE = "run_meta.json"


def run_within_budget[T](
    items: Iterable[T],
    run: Callable[[T], TaskRecord],
    spent_usd: Callable[[], float],
    max_usd: float | None,
) -> tuple[list[TaskRecord], bool]:
    """Run items in order; stop before the next one at the limit. The bool says the result is partial."""
    records: list[TaskRecord] = []
    for item in items:
        if max_usd is not None and spent_usd() >= max_usd:
            return records, True
        try:
            records.append(run(item))
        except BudgetExceededError:
            return records, True
    return records, False


def write_meta(out_dir: Path, *, partial: bool, spent_usd: float, max_usd: float | None) -> None:
    meta = {"partial": partial, "spent_usd": round(spent_usd, 6), "max_usd": max_usd}
    (out_dir / META_FILE).write_text(json.dumps(meta), encoding="utf-8")


def is_partial(out_dir: Path) -> bool:
    try:
        return bool(json.loads((out_dir / META_FILE).read_text(encoding="utf-8")).get("partial"))
    except (OSError, ValueError, AttributeError):
        return False
