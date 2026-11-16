"""Suite A: the SDK on real Wikipedia in real Chromium, against a deployed Atlas.

Goal "Open the Wikipedia article about {topic}". Two arms on the same pages and the same model:
  baseline  the plain agent: DeepSeek picks every browser step from the page snapshot
  atlas     the SDK: one /v1/start, the known road with no model call, DeepSeek only at the frontier

A process run takes a list of browser contexts; each context is fresh and runs its topics in order.
Run the script twice (two processes) on the same suite key to test reuse across process runs:

    python -m eval.live.sdk_wiki --process p1 --context 1:first --context 2:repeat
    python -m eval.live.sdk_wiki --process p2 --context 3:repeat
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from atlas_sdk import Atlas
from atlas_sdk.playwright import PlaywrightDriver
from playwright.sync_api import sync_playwright

from atlas.contracts.errors import BudgetExceededError
from eval.live.common import (
    LIVE_URL,
    WIKI_HOME,
    CountingModel,
    Explorer,
    Json,
    Record,
    build_model,
    on_article,
    out,
    suite_key,
)

GOAL = "Open the Wikipedia article about {topic}"
SITE = "en.wikipedia.org"
TOPICS = (
    "Ada Lovelace", "Alan Turing", "Marie Curie", "Mount Everest", "Shah Rukh Khan",
    "Hrithik Roshan", "Grace Hopper", "Lake Baikal", "Nikola Tesla", "Kathmandu",
)  # fmt: skip
REPEATS = {2: TOPICS[:5], 3: TOPICS[5:]}  # context -> topics repeated in it (5 each)
MAX_STEPS = 8


class CountingAtlas(Atlas):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.calls = 0

    def post(self, path: str, body: Json) -> Json:
        self.calls += 1
        return super().post(path, body)


def run_baseline(page: Any, model: CountingModel, topic: str) -> tuple[bool, int]:  # noqa: ANN401
    page.goto(WIKI_HOME)
    agent = Explorer(model, PlaywrightDriver(page), GOAL, {"topic": topic})
    agent.run(MAX_STEPS)
    return on_article(page.url, topic), agent.steps


def run_atlas(page: Any, model: CountingModel, atlas: CountingAtlas, topic: str) -> tuple[bool, Any]:  # noqa: ANN401
    page.goto(WIKI_HOME)
    driver = PlaywrightDriver(page)
    agent = Explorer(model, driver, GOAL, {"topic": topic})

    def fallback(snap: Json, _goal: str, _params: Json) -> Json | None:
        return agent.decide(snap)

    summary = atlas.session(driver).run(
        GOAL, {"topic": topic}, fallback=fallback, max_steps=MAX_STEPS, kind="read"
    )
    return on_article(page.url, topic), summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default=LIVE_URL)
    p.add_argument("--out", type=Path, default=Path("results/live-2026-11-16"))
    p.add_argument("--process", default="p1")
    p.add_argument("--context", action="append", required=True, help="N:first or N:repeat")
    p.add_argument("--arms", default="baseline,atlas")
    p.add_argument("--max-usd", type=float, default=1.50)
    p.add_argument("--key-name", default="A", help="suite key tag; a new tag creates a new key")
    p.add_argument("--topics", default="", help="comma list overriding the topic list (smoke runs)")
    a = p.parse_args()
    model = build_model(a.max_usd)
    atlas = CountingAtlas(a.base, suite_key(a.key_name, a.base), site=SITE)
    records = a.out / "records.jsonl"
    seen_first = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for spec in a.context:
                number, mode = spec.split(":")
                ctx = browser.new_context(locale="en-US")
                topics = TOPICS if mode == "first" else REPEATS[int(number)]
                if a.topics:
                    topics = tuple(a.topics.split(","))
                for topic in topics:
                    if model.ledger.status() == "exhausted":
                        out("live: spend cap reached, stopping")
                        return 1
                    scenario = "repeat" if mode == "repeat" else ("new_subject" if seen_first else "first")
                    seen_first = True
                    for arm in a.arms.split(","):
                        page = ctx.new_page()
                        calls0, usd0, atlas0, t0 = (
                            model.calls,
                            model.ledger.spent_usd,
                            atlas.calls,
                            time.monotonic(),
                        )
                        try:
                            if arm == "baseline":
                                ok, steps = run_baseline(page, model, topic)
                                via = reason = ""
                            else:
                                ok, s = run_atlas(page, model, atlas, topic)
                                steps, via, reason = s.steps, s.via, s.reason or ""
                        except BudgetExceededError:
                            out("live: spend cap reached, stopping")
                            return 1
                        wall = time.monotonic() - t0
                        Record(
                            suite="A", arm=arm, scenario=scenario, subject=topic, context=int(number),
                            process=a.process, success=ok, model_calls=model.calls - calls0,
                            atlas_calls=atlas.calls - atlas0, browser_steps=steps,
                            turns=steps + atlas.calls - atlas0, wall_s=round(wall, 2),
                            usd=round(model.ledger.spent_usd - usd0, 6), via=via, reason=reason,
                            landed=page.url,
                        ).write(records)  # fmt: skip
                        page.close()
                ctx.close()
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
