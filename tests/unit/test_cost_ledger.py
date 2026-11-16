"""Spend ledger and budget cap (cost guard). Targets (lane G), in atlas/adapters/model/ledger.py:

    SpendLedger(path: Path, budget_usd: float, price_in_per_m: float, price_out_per_m: float,
                reserve_usd: float = 0.002)
        .spent_usd -> float      .calls -> int
        .check() -> None         raises BudgetExceededError when spent_usd + reserve_usd > budget_usd
        .charge(input_tokens: int, output_tokens: int) -> float     cost of this call, now recorded
        .status() -> "ok" | "low" | "exhausted"   exhausted: check() would raise; low: under 20% left
    BudgetedModelClient(inner: ModelClient, ledger)     # ModelClient
    BudgetedToolModel(inner: ToolModel, ledger)         # ToolModel
    ledger_from_settings(settings: Settings) -> SpendLedger

Cost = (input_tokens * price_in_per_m + output_tokens * price_out_per_m) / 1_000_000 (all input
at the cache-miss price). The ledger file is JSON; every charge re-reads it under the lock and
writes it atomically, so restarts and several processes sharing one path add up.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("atlas.adapters.model.ledger", reason="cost guard: implemented by lane G")

from hypothesis import given
from hypothesis import strategies as st

from atlas.adapters.model.ledger import (
    BudgetedModelClient,
    BudgetedToolModel,
    SpendLedger,
    ledger_from_settings,
)
from atlas.contracts.bench_models import ToolTurn
from atlas.contracts.errors import BudgetExceededError
from atlas.contracts.types import ModelDecision
from atlas.settings import Settings
from tests.support.builders import page

SNAPSHOT = page("/a", "button:go")


def ledger(tmp_path, budget: float = 1.0, **options) -> SpendLedger:
    return SpendLedger(tmp_path / "spend" / "ledger.json", budget, 0.30, 1.20, **options)


class CountingModel:
    def __init__(self, input_tokens: int = 1000, output_tokens: int = 100) -> None:
        self.calls, self.usage = 0, (input_tokens, output_tokens)

    def next_action(self, goal_text, params, snapshot, history) -> ModelDecision:
        self.calls += 1
        return ModelDecision(action=None, done=True, input_tokens=self.usage[0], output_tokens=self.usage[1])

    def turn(self, task_text, tools, history) -> ToolTurn:
        self.calls += 1
        return ToolTurn(text="done", input_tokens=self.usage[0], output_tokens=self.usage[1])


@given(st.integers(0, 2_000_000), st.integers(0, 200_000))
def test_a_call_costs_usage_times_price(tmp_path_factory, tokens_in, tokens_out):
    """R-COST-1: cost = input * 0.30/M + output * 1.20/M, and the ledger adds it up."""
    book = ledger(tmp_path_factory.mktemp("ledger"), budget=10_000)
    expected = (tokens_in * 0.30 + tokens_out * 1.20) / 1_000_000
    assert book.charge(tokens_in, tokens_out) == pytest.approx(expected)
    assert book.charge(tokens_in, tokens_out) == pytest.approx(expected)
    assert book.spent_usd == pytest.approx(2 * expected)
    assert book.calls == 2


def test_spend_survives_restarts_and_is_shared_by_instances(tmp_path):
    """R-COST-1: the ledger is a JSON file; a new instance, or a second one on the same path,
    continues from what was already spent."""
    first = ledger(tmp_path)
    first.charge(1_000_000, 0)
    second = ledger(tmp_path)
    assert second.spent_usd == pytest.approx(0.30)
    second.charge(0, 100_000)
    first.charge(1_000_000, 0)
    assert ledger(tmp_path).spent_usd == pytest.approx(0.30 + 0.12 + 0.30)
    stored = json.loads((tmp_path / "spend" / "ledger.json").read_text(encoding="utf-8"))
    assert stored["spent_usd"] == pytest.approx(0.72)
    assert stored["calls"] == 3


def test_concurrent_charges_are_not_lost(tmp_path):
    """R-COST-1: 200 charges from 16 threads add up exactly."""
    book = ledger(tmp_path, budget=100)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: book.charge(10_000, 1_000), range(200)))
    assert book.calls == 200
    assert book.spent_usd == pytest.approx(200 * (0.003 + 0.0012))
    assert ledger(tmp_path, budget=100).spent_usd == pytest.approx(book.spent_usd)


def test_the_cap_stops_calls_before_the_provider_is_reached(tmp_path):
    """R-COST-1: once the next call could exceed the budget, the wrapped model is not called
    and a typed BudgetExceededError is raised; this holds after a restart too."""
    book = ledger(tmp_path, budget=0.01, reserve_usd=0.002)
    inner = CountingModel(input_tokens=10_000, output_tokens=0)  # $0.003 per call
    model = BudgetedModelClient(inner, book)
    for _ in range(3):
        model.next_action("goal", {}, SNAPSHOT, ())
    assert (inner.calls, book.status()) == (3, "exhausted")  # $0.009 spent, $0.002 reserve does not fit
    with pytest.raises(BudgetExceededError):
        model.next_action("goal", {}, SNAPSHOT, ())
    assert inner.calls == 3
    assert book.spent_usd == pytest.approx(0.009)

    restarted = BudgetedToolModel(inner, ledger(tmp_path, budget=0.01, reserve_usd=0.002))
    with pytest.raises(BudgetExceededError):
        restarted.turn("task", (), ())
    assert inner.calls == 3


def test_tool_model_turns_are_charged_too(tmp_path):
    """R-COST-1: the benchmark's tool-calling model goes through the same ledger."""
    book = ledger(tmp_path)
    turn = BudgetedToolModel(CountingModel(2000, 500), book).turn("task", (), ())
    assert turn.text == "done"
    assert book.spent_usd == pytest.approx((2000 * 0.30 + 500 * 1.20) / 1_000_000)


def test_status_is_coarse(tmp_path):
    """R-COST-3: status is ok, then low under 20% remaining, then exhausted; never an amount."""
    book = ledger(tmp_path, budget=1.0, reserve_usd=0.01)
    assert book.status() == "ok"
    book.charge(2_700_000, 0)  # $0.81 spent
    assert book.status() == "low"
    book.charge(630_000, 0)  # $0.999 spent
    assert book.status() == "exhausted"
    with pytest.raises(BudgetExceededError):
        book.check()


def test_settings_carry_the_prices_cap_and_path(tmp_path):
    """R-COST-1: defaults are $0.30 / $1.20 per million and a hard cap; all come from settings."""
    defaults = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (defaults.model_price_in_per_m, defaults.model_price_out_per_m) == (0.30, 1.20)
    assert 0 < defaults.model_budget_usd <= 5.0
    settings = Settings(_env_file=None, model_ledger_path=str(tmp_path / "l.json"), model_budget_usd=0.5)  # type: ignore[call-arg]
    book = ledger_from_settings(settings)
    book.charge(1_000_000, 0)
    assert (tmp_path / "l.json").exists()
    assert book.spent_usd == pytest.approx(0.30)
    book.charge(1_000_000, 0)
    assert book.status() == "exhausted"
