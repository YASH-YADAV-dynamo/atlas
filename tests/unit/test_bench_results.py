"""Benchmark results: schema, JSON-lines storage and aggregation. Targets (lane B):
eval.results.append_records(path: Path, records: Iterable[TaskRecord]) -> None
eval.results.read_records(path: Path) -> list[TaskRecord]
eval.results.summarise(records: Sequence[TaskRecord]) -> list[SummaryRow]
"""

from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

pytest.importorskip("eval.results", reason="benchmark wave: implemented by lane B")

from hypothesis import given
from hypothesis import strategies as st

from atlas.contracts.bench_models import Arm, Split, TaskRecord
from eval.results import append_records, read_records, summarise


def record(**changes) -> TaskRecord:
    base = {
        "run_id": "run-1",
        "arm": Arm.ATLAS,
        "seed": 1,
        "iteration": 0,
        "split": Split.STREAM,
        "template_id": "refund_order",
        "success": True,
        "model_calls": 2,
        "input_tokens": 200,
        "output_tokens": 20,
        "actions": 5,
        "wall_ms": 1000,
    }
    return TaskRecord(**{**base, **changes})


RECORDS = st.lists(
    st.builds(
        record,
        arm=st.sampled_from(list(Arm)),
        split=st.sampled_from([Split.STREAM, Split.HELDOUT]),
        iteration=st.integers(0, 2),
        success=st.booleans(),
        model_calls=st.integers(0, 30),
        input_tokens=st.integers(0, 50_000),
        output_tokens=st.integers(0, 5_000),
        tool_calls=st.integers(0, 40),
        context_tokens=st.integers(0, 60_000),
        wall_ms=st.integers(0, 120_000),
    ),
    max_size=40,
)


def test_records_round_trip_as_json_lines(tmp_path):
    """R-BENCH-2: results are appended as one JSON object per line and read back unchanged."""
    path = tmp_path / "results" / "records.jsonl"
    first, second = [record(), record(arm=Arm.B0, success=False)], [record(arm=Arm.M1, tool_calls=3)]
    append_records(path, first)
    append_records(path, second)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert read_records(path) == [*first, *second]


def test_record_schema_refuses_free_text_and_bad_values():
    """R-BENCH-2: a record cannot carry prompts, page text or secrets, or impossible numbers."""
    for bad in ({"prompt": "x"}, {"page_text": "x"}, {"model_calls": -1}, {"arm": "b9"}, {"run_id": "a b"}):
        with pytest.raises(ValidationError):
            record(**bad)


@given(RECORDS)
def test_summary_is_derived_only_from_the_records(records):
    """R-BENCH-3: every reported number is recomputable from the raw records."""
    rows = summarise(records)
    groups: dict[tuple, list[TaskRecord]] = {}
    for item in records:
        groups.setdefault((item.arm, item.split, item.iteration), []).append(item)
    assert [(r.arm, r.split, r.iteration) for r in rows] == sorted(
        groups, key=lambda key: (key[0].value, key[1].value, key[2])
    )
    for row in rows:
        group = groups[(row.arm, row.split, row.iteration)]
        wins = [item for item in group if item.success]
        assert (row.tasks, row.successes) == (len(group), len(wins))
        assert row.success_rate == pytest.approx(len(wins) / len(group))
        assert row.zero_model_call_share == pytest.approx(sum(w.model_calls == 0 for w in wins) / len(group))
        assert row.tool_calls_per_task == pytest.approx(sum(i.tool_calls for i in group) / len(group))
        assert row.context_tokens_per_task == pytest.approx(sum(i.context_tokens for i in group) / len(group))
        if wins:
            assert row.model_calls_per_success == pytest.approx(sum(w.model_calls for w in wins) / len(wins))
            assert row.tokens_per_success == pytest.approx(
                sum(w.input_tokens + w.output_tokens for w in wins) / len(wins)
            )
        else:
            assert row.model_calls_per_success is None and row.tokens_per_success is None
        times = sorted(item.wall_ms for item in group)
        assert row.wall_ms_p50 == times[math.ceil(0.5 * len(times)) - 1]
        assert row.wall_ms_p95 == times[math.ceil(0.95 * len(times)) - 1]


def test_failed_tasks_do_not_flatter_the_cost_per_success():
    """R-BENCH-3: cost per successful task ignores failed tasks' calls; success rate does not."""
    rows = summarise([record(model_calls=4), record(success=False, model_calls=40), record(model_calls=0)])
    assert len(rows) == 1
    assert (rows[0].tasks, rows[0].successes) == (3, 2)
    assert rows[0].model_calls_per_success == pytest.approx(2.0)
    assert rows[0].success_rate == pytest.approx(2 / 3)
    assert rows[0].zero_model_call_share == pytest.approx(1 / 3)
