"""Spend ledger with a hard cap: every model call is checked before and charged after (R-COST-1).

One JSON file `{spent_usd, calls, input_tokens, output_tokens}`. Each charge re-reads the file under
a lock and replaces it atomically, so restarts and several processes on one path add up. All input
tokens are charged at the cache-miss price. Amounts are never logged.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from atlas.contracts.bench_models import ToolDef, ToolModel, ToolResult, ToolTurn
from atlas.contracts.errors import BudgetExceededError
from atlas.contracts.ports import ModelClient
from atlas.contracts.types import ModelDecision, Params, Snapshot
from atlas.settings import Settings

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOW_FRACTION = 0.20
Status = Literal["ok", "low", "exhausted"]
_FIELDS = ("spent_usd", "calls", "input_tokens", "output_tokens")
_THREAD_LOCK = threading.Lock()  # serialises threads; the lock file serialises processes


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK, open(path.with_name(path.name + ".lock"), "a+b") as handle:
        if sys.platform == "win32":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # blocks, retrying for about 10 s
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SpendLedger:
    def __init__(
        self,
        path: Path,
        budget_usd: float,
        price_in_per_m: float,
        price_out_per_m: float,
        reserve_usd: float = 0.002,
    ) -> None:
        self._path = Path(path)
        self._budget = budget_usd
        self._price_in, self._price_out = price_in_per_m, price_out_per_m
        self._reserve = reserve_usd

    def _read(self) -> dict[str, float]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {key: float(raw.get(key, 0)) for key in _FIELDS}
        except FileNotFoundError:
            return dict.fromkeys(_FIELDS, 0.0)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # an unreadable ledger fails closed: never treated as zero spend
            raise BudgetExceededError("model spend ledger is unreadable") from exc

    @property
    def spent_usd(self) -> float:
        with _locked(self._path):
            return self._read()["spent_usd"]

    @property
    def calls(self) -> int:
        with _locked(self._path):
            return int(self._read()["calls"])

    def check(self) -> None:
        if self.status() == "exhausted":
            raise BudgetExceededError("model budget exhausted")

    def charge(self, input_tokens: int, output_tokens: int) -> float:
        cost = (input_tokens * self._price_in + output_tokens * self._price_out) / 1_000_000
        with _locked(self._path):
            data = self._read()
            data["spent_usd"] += cost
            data["calls"] += 1
            data["input_tokens"] += input_tokens
            data["output_tokens"] += output_tokens
            stored = {k: (data[k] if k == "spent_usd" else int(data[k])) for k in _FIELDS}
            temp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(stored), encoding="utf-8")
            os.replace(temp, self._path)
        return cost

    def status(self) -> Status:
        try:
            spent = self.spent_usd
        except BudgetExceededError:
            return "exhausted"
        if spent + self._reserve > self._budget:
            return "exhausted"
        return "low" if self._budget - spent < LOW_FRACTION * self._budget else "ok"


class BudgetedModelClient:
    """`ModelClient` that refuses to call the provider once the cap is reached."""

    def __init__(self, inner: ModelClient, ledger: SpendLedger) -> None:
        self._inner, self._ledger = inner, ledger

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        self._ledger.check()
        decision = self._inner.next_action(goal_text, params, snapshot, history)
        self._ledger.charge(decision.input_tokens, decision.output_tokens)
        return decision


class BudgetedToolModel:
    """`ToolModel` behind the same ledger."""

    def __init__(self, inner: ToolModel, ledger: SpendLedger) -> None:
        self._inner, self._ledger = inner, ledger

    def turn(
        self, task_text: str, tools: tuple[ToolDef, ...], history: tuple[ToolTurn | ToolResult, ...]
    ) -> ToolTurn:
        self._ledger.check()
        turn = self._inner.turn(task_text, tools, history)
        self._ledger.charge(turn.input_tokens, turn.output_tokens)
        return turn


def ledger_from_settings(settings: Settings) -> SpendLedger:
    return SpendLedger(
        Path(settings.model_ledger_path),
        settings.model_budget_usd,
        settings.model_price_in_per_m,
        settings.model_price_out_per_m,
    )
