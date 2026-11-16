"""Shared pieces of the live suites: the budgeted model, the plain agent loop, keys and records.

The model is the configured exploration model (DeepSeek through the OpenAI-compatible client). Every
call is charged to the project ledger and to a separate live-test ledger whose budget is `--max-usd`,
so the live test stops at its own cap. Keys are kept in a file outside the repo and never printed.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from atlas_sdk.client import as_fill, bind_action, fold_fills
from pydantic import ValidationError

from atlas.adapters.model.client import SYSTEM, TOOL_NAME, build_prompt
from atlas.adapters.model.factory import model_api_key, model_base_url
from atlas.adapters.model.ledger import BudgetedModelClient, SpendLedger, ledger_from_settings
from atlas.adapters.model.openai_compat import (
    OpenAICompatModelClient,
    first_json_object,
    post_chat,
    usage_of,
)
from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.ports import ModelClient
from atlas.contracts.types import Action, ModelDecision, Params, Snapshot
from atlas.core.snapshot import normalise_raw
from atlas.runner.run import describe
from atlas.settings import Settings

Json = dict[str, Any]
LIVE_URL = "https://atlas.yashdynamo.fun"
WIKI_HOME = "https://en.wikipedia.org/wiki/Main_Page"
KEY_FILE = Path("D:/hydradb-hack/.secrets/live_keys_l7.json")  # outside the repo, never printed
LIVE_LEDGER = Path("data/live_ledger.json")  # data/ is gitignored
HISTORY_LINES = 6


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class CountingModel:
    """The budgeted model, counting calls and the spend it caused."""

    def __init__(self, inner: ModelClient, ledger: SpendLedger) -> None:
        self._inner, self.ledger = inner, ledger
        self.calls = 0

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        self.calls += 1  # a failed call counts: the provider may still bill it (the ledger cannot)
        return self._inner.next_action(goal_text, params, snapshot, history)


class LiveModelClient(OpenAICompatModelClient):
    """The exploration client, with one harness fix: DeepSeek writes long answers for research goals
    and `ModelDecision.answer` takes 200 characters, so a valid "done" was rejected as an invalid
    decision. The answer is cut to 200 before validation; nothing else changes."""

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        payload = {
            "model": self._model_id,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": build_prompt(goal_text, params, snapshot, history)},
            ],
            "tools": [self._tool],
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
            "thinking": {"type": "disabled"},
        }
        body = post_chat(self._http, self._base_url, self._api_key, payload)
        try:
            arguments = first_json_object(
                body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelUnavailableError("model returned no tool call") from exc
        if not isinstance(arguments, dict):
            raise ModelUnavailableError("model returned no tool call")
        if "done" not in arguments:
            arguments = {**arguments, "done": arguments.get("action") is None}
        if isinstance(arguments.get("answer"), str):
            arguments["answer"] = arguments["answer"][:200]
        tokens_in, tokens_out = usage_of(body)
        try:
            return ModelDecision.model_validate(
                {**arguments, "input_tokens": tokens_in, "output_tokens": tokens_out}
            )
        except ValidationError as exc:
            raise ModelUnavailableError(
                f"model returned an invalid decision: {exc.errors()[0]['loc']}"
            ) from exc


def build_model(max_usd: float) -> CountingModel:
    settings = Settings()
    if settings.model_provider != "openai":
        raise SystemExit("live: expected MODEL_PROVIDER=openai (DeepSeek)")
    provider = LiveModelClient(model_api_key(settings), settings.atlas_model_id, model_base_url(settings))
    live = SpendLedger(LIVE_LEDGER, max_usd, settings.model_price_in_per_m, settings.model_price_out_per_m)
    inner = BudgetedModelClient(BudgetedModelClient(provider, ledger_from_settings(settings)), live)
    return CountingModel(inner, live)


def suite_key(name: str, base: str) -> str:
    """One private key per suite, reused across process runs. Created once, never printed."""
    keys: dict[str, str] = json.loads(KEY_FILE.read_text()) if KEY_FILE.exists() else {}
    tag = f"{base}|{name}"
    if tag not in keys:
        response = httpx.post(f"{base}/v1/keys", timeout=30)
        response.raise_for_status()
        keys[tag] = response.json()["api_key"]
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(json.dumps(keys))
    return keys[tag]


def as_snapshot(raw: Json) -> Snapshot:
    return Snapshot.model_validate(normalise_raw(raw))


def dump(action: Action) -> Json:
    return action.model_dump(mode="json", exclude_none=True)


@dataclass
class Explorer:
    """The plain agent: one model call per browser step, from the live page snapshot, until done.

    Same-page fills are folded into the action that follows, as the SDK does, so a batched report
    carries whole steps. `log` holds the steps for that report."""

    model: CountingModel
    driver: Any
    goal: str
    params: dict[str, str]
    history: list[str] = field(default_factory=list)
    log: list[Json] = field(default_factory=list)
    steps: int = 0
    claimed_done: bool = False

    def decide(self, snap: Json) -> Json | None:
        """One model decision on this page: the action as a dict, or None when the model says done."""
        decision = None
        for _ in range(2):  # one retry on an unusable reply, as an agent loop would
            try:
                decision = self.model.next_action(
                    self.goal, self.params, as_snapshot(snap), tuple(self.history[-HISTORY_LINES:])
                )
                break
            except ModelUnavailableError as exc:
                out(f"live: model error: {exc}")
        if decision is None:
            return None
        if decision.done or decision.action is None:
            self.claimed_done = True
            return None
        self.history.append(describe(decision.action))
        return dump(decision.action)

    def run(self, max_steps: int, first: Json | None = None) -> None:
        """Explore from the current page; `first` is an action already decided on this page."""
        held: list[Json] = []
        snap = self.driver.snapshot()
        while self.steps < max_steps:
            action, first = first or self.decide(snap), None
            if action is None:
                return
            before, started, ok = snap["url"], time.monotonic(), True
            self.steps += 1
            try:
                self.driver.act(bind_action(action, self.params))
            except Exception:  # a failed browser step is fed back to the model as history
                ok = False
                self.history[-1] = "FAILED: " + self.history[-1]
            snap = self.driver.snapshot()
            fills = same_page_fills(action)
            if ok and fills and snap["url"] == before:
                mine = {json.dumps(f["locator"], sort_keys=True) for f in fills}
                held = [f for f in held if json.dumps(f["locator"], sort_keys=True) not in mine] + fills
                continue
            ms = min(int((time.monotonic() - started) * 1000), 600_000)
            self.log.append({"action": fold_fills(action, held), "to": snap, "ok": ok, "ms": ms})
            held = []


def same_page_fills(action: Json) -> list[Json]:
    """A fill/select/check action as plain fills (its own nested fills first), else []. DeepSeek
    sometimes sends "fill the searchbox" with the same fill nested inside it."""
    if action["kind"] not in ("fill", "select", "check"):
        return []
    own = as_fill({key: val for key, val in action.items() if key != "fills"})
    return [*action.get("fills", ()), *([own] if own else [])]


def wiki_path(url: str) -> str:
    return unquote(urlsplit(url).path).replace(" ", "_").lower()


def on_article(url: str, title: str) -> bool:
    return wiki_path(url) == "/wiki/" + title.replace(" ", "_").lower()


@dataclass
class Record:
    suite: str
    arm: str  # "baseline" | "atlas"
    scenario: str  # first | new_subject | repeat | reworded
    subject: str
    context: int
    process: str
    success: bool
    model_calls: int
    atlas_calls: int
    browser_steps: int
    turns: int  # tool calls an MCP agent makes: browser steps plus Atlas calls
    wall_s: float
    usd: float
    via: str = ""
    reason: str = ""
    landed: str = ""

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(self)) + "\n")
        out(
            f"{self.suite} {self.arm:8} {self.scenario:11} {self.subject!r:24} ctx={self.context} "
            f"ok={self.success} model={self.model_calls} atlas={self.atlas_calls} steps={self.browser_steps} "
            f"turns={self.turns} {self.wall_s:.1f}s ${self.usd:.4f} via={self.via} reason={self.reason}"
        )


def timed[T](fn: Callable[[], T]) -> tuple[T, float]:
    started = time.monotonic()
    value = fn()
    return value, time.monotonic() - started
