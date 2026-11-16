"""Suite B: research through the Atlas MCP tools on real Wikipedia in real Chromium.

Goal "Find the awards of {person}" (kind read). The answer page is the person's
"List of awards and nominations received by ..." article; success is checked on the landing URL.

  baseline  the plain agent: DeepSeek picks every browser step from the page snapshot
  atlas     the same agent with the Atlas MCP tools (the two-call flow): atlas_plan first; follow
            `open_url` or the route with no model call; DeepSeek only at the frontier; one batched
            atlas_report after exploring (skipped after a clean replay, founder choice F2)

`turns` counts the tool calls an MCP agent would make: browser steps plus Atlas calls. A route is
replayed step by step (no script tool), so a route of n steps costs n + 1 turns.

    python -m eval.live.mcp_research
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import httpx
from atlas_sdk.client import bind_action
from atlas_sdk.playwright import PlaywrightDriver
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from playwright.sync_api import sync_playwright

from atlas.contracts.errors import BudgetExceededError
from atlas.contracts.types import Action
from atlas.runner.run import describe
from eval.live.common import (
    LIVE_URL,
    WIKI_HOME,
    CountingModel,
    Explorer,
    Json,
    Record,
    build_model,
    out,
    suite_key,
    wiki_path,
)

SITE = "en.wikipedia.org"  # --site overrides it, to isolate a run on a key that already has a map
# The suite needs its own key: on a key that already holds a similar goal, a new wording is filed
# under the old goal and stays a miss (see RESULTS.md).
GOAL = "Find the awards of {person}"
REWORDED = "Show the awards of {person}"  # same words (tier 2) as GOAL
# (scenario, goal template, person), in order
TASKS = (
    ("first", GOAL, "Hrithik Roshan"),
    ("new_subject", GOAL, "Shah Rukh Khan"),
    ("repeat", GOAL, "Hrithik Roshan"),
    ("new_subject", GOAL, "Priyanka Chopra"),
    ("reworded", REWORDED, "Shah Rukh Khan"),
)
MAX_STEPS = 8


def on_awards(url: str, person: str) -> bool:
    path = wiki_path(url)
    return path.startswith("/wiki/list_of_awards") and path.endswith(person.replace(" ", "_").lower())


def summary(snap: Json) -> Json:
    """What an MCP agent sends: url and title only (recommended input)."""
    return {"url": snap["url"], "title": snap["title"]}


class SyncMcp:
    """The Atlas MCP client in one task on its own event loop thread (anyio scopes must enter and
    exit in the same task), so the sync Playwright API can run in the main thread."""

    def __init__(self, base: str, key: str, debug: Path | None = None) -> None:
        self._loop = asyncio.new_event_loop()
        self._base, self._key, self._debug = base, key, debug
        self._jobs: asyncio.Queue[tuple[str, Json, Future[Any]] | None] | None = None
        self._ready: Future[None] = Future()
        self.calls = 0
        threading.Thread(target=self._loop.run_until_complete, args=(self._serve(),), daemon=True).start()
        self._ready.result(timeout=60)

    async def _serve(self) -> None:
        self._jobs = asyncio.Queue()
        try:
            headers = {"Authorization": f"Bearer {self._key}"}
            async with (
                create_mcp_http_client(headers=headers) as http,
                Client(streamable_http_client(f"{self._base}/mcp", http_client=http)) as client,
            ):
                self._ready.set_result(None)
                while (job := await self._jobs.get()) is not None:
                    name, arguments, done = job
                    try:
                        done.set_result(await client.call_tool(name, arguments))
                    except Exception as exc:  # handed to the caller
                        done.set_exception(exc)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)

    def call(self, name: str, arguments: Json) -> Json:
        self.calls += 1
        done: Future[Any] = Future()
        assert self._jobs is not None
        self._loop.call_soon_threadsafe(self._jobs.put_nowait, (name, arguments, done))
        result = done.result(timeout=120)
        text = result.content[0].text
        if self._debug is not None:
            with self._debug.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"tool": name, "in": arguments, "out": text[:3000]}) + "\n")
        if result.is_error:
            raise RuntimeError(f"{name}: {text[:300]}")
        return json.loads(text)

    def close(self) -> None:
        if self._jobs is not None:
            self._loop.call_soon_threadsafe(self._jobs.put_nowait, None)


def run_baseline(page: Any, model: CountingModel, goal: str, person: str) -> tuple[bool, int, str]:  # noqa: ANN401
    page.goto(WIKI_HOME)
    agent = Explorer(model, PlaywrightDriver(page), goal, {"person": person})
    agent.run(MAX_STEPS)
    return on_awards(page.url, person), agent.steps, ""


def run_atlas(
    page: Any,  # noqa: ANN401
    model: CountingModel,
    mcp: SyncMcp,
    goal: str,
    person: str,
    site: str,
) -> tuple[bool, int, str]:
    page.goto(WIKI_HOME)
    driver = PlaywrightDriver(page)
    params = {"person": person}
    snap = driver.snapshot()
    plan = mcp.call(
        "atlas_plan", {"site": site, "snapshot_summary": summary(snap), "goal_text": goal, "params": params}
    )
    note = f"{plan.get('reason')}/{plan.get('match')}"
    params = {**params, **(plan.get("params") or {})}
    goal = plan.get("goal") or goal
    steps = 0
    road = (
        [{"action": {"kind": "goto", "value": plan["open_url"]}}]
        if plan.get("open_url")
        else plan.get("route", [])
    )
    agent = Explorer(model, driver, goal, params)
    from_state, first = plan["state_id"], None
    if not plan.get("needs_model") and road:
        done = 0
        for step in road:
            steps += 1
            bound = bind_action(step["action"], params)
            try:
                driver.act(bound)
            except Exception:  # a failed replay step goes to the frontier below
                agent.history.append("FAILED: " + describe(Action.model_validate(bound)))
                break
            agent.history.append(describe(Action.model_validate(bound)))  # the agent knows what it ran
            done += 1
        snap = driver.snapshot()
        via = "url" if plan.get("open_url") else "route"
        first = agent.decide(snap)  # the agent reads the page it was sent to, as the baseline does
        if first is None and agent.claimed_done:
            return on_awards(page.url, person), steps, f"{via} {note}"  # clean replay: report skipped (F2)
        if done == len(road):  # every step ran but the page is not the answer: blame the last one
            done = max(done - 1, 0)
        out_ = mcp.call(
            "atlas_report",
            {"site": site, "plan_id": plan["plan_id"], "ok": False, "to_snapshot_summary": summary(snap),
             "done": 0 if plan.get("open_url") else done},
        )  # fmt: skip
        from_state, note = out_["state_id"], f"{note} replay-failed"
    agent.run(MAX_STEPS - steps, first)
    if agent.log:
        mcp.call(
            "atlas_report",
            {"site": site, "from_state": from_state, "ok": True, "goal_text": goal, "params": params,
             "goal_reached": agent.claimed_done, "kind": "read",
             "steps": [{**s, "to": summary(s["to"])} for s in agent.log[:12]]},
        )  # fmt: skip
    return on_awards(page.url, person), steps + agent.steps, f"explore {note}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=LIVE_URL)
    p.add_argument("--out", type=Path, default=Path("results/live-2026-11-16"))
    p.add_argument("--key-name", default="B")
    p.add_argument("--process", default="clean")
    p.add_argument("--site", default=SITE)
    p.add_argument("--arms", default="baseline,atlas")
    p.add_argument("--max-usd", type=float, default=1.50)
    a = p.parse_args()
    model = build_model(a.max_usd)
    mcp = SyncMcp(a.base, suite_key(a.key_name, a.base), a.out / "mcp_debug.jsonl")
    records = a.out / "records.jsonl"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(locale="en-US")
            for scenario, goal, person in TASKS:
                for arm in a.arms.split(","):
                    if model.ledger.status() == "exhausted":
                        out("live: spend cap reached, stopping")
                        return 1
                    page = ctx.new_page()
                    calls0, usd0, atlas0, t0 = (
                        model.calls,
                        model.ledger.spent_usd,
                        mcp.calls,
                        time.monotonic(),
                    )
                    try:
                        if arm == "baseline":
                            ok, steps, via = run_baseline(page, model, goal, person)
                        else:
                            ok, steps, via = run_atlas(page, model, mcp, goal, person, a.site)
                    except BudgetExceededError:
                        out("live: spend cap reached, stopping")
                        return 1
                    except (RuntimeError, httpx.HTTPError) as exc:
                        out(f"live: {arm} failed: {exc}")
                        ok, steps, via = False, 0, f"error {type(exc).__name__}"
                    Record(
                        suite="B", arm=arm, scenario=scenario, subject=person, context=1, process=a.process,
                        success=ok, model_calls=model.calls - calls0, atlas_calls=mcp.calls - atlas0,
                        browser_steps=steps, turns=steps + mcp.calls - atlas0,
                        wall_s=round(time.monotonic() - t0, 2), usd=round(model.ledger.spent_usd - usd0, 6),
                        via=via.split(" ")[0], reason=via.split(" ", 1)[-1], landed=page.url,
                    ).write(records)  # fmt: skip
                    page.close()
            browser.close()
    finally:
        mcp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
