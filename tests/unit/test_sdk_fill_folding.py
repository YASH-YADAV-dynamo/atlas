"""C7 (SHIP_PLAN 4.3): the SDK folds standalone form fills into the action that follows them on
the same page before reporting, so the finish batch carries them inside one step."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("atlas_sdk")

from atlas_sdk import Atlas
from atlas_sdk.client import as_fill, fold_fills

BOX = {"role": "searchbox", "name": "Search"}
GO = {"role": "button", "name": "Go"}
FILL = {"locator": BOX, "kind": "fill", "param": "topic"}


def test_as_fill_recognises_standalone_fills_only():
    assert as_fill({"kind": "fill", "locator": BOX, "value": "x"}) == {
        "locator": BOX,
        "kind": "fill",
        "value": "x",
    }
    assert as_fill({"kind": "check", "locator": BOX}) == {"locator": BOX, "kind": "check", "value": "true"}
    assert as_fill({"kind": "click", "locator": GO}) is None
    assert as_fill({"kind": "fill", "locator": BOX, "value": "x", "fills": [FILL]}) is None


def test_fold_puts_held_fills_first_and_the_actions_own_fill_wins():
    other = {"locator": {"role": "checkbox", "name": "Exact"}, "kind": "check", "value": "true"}
    click = {"kind": "click", "locator": GO, "fills": [other]}
    assert fold_fills(click, [FILL])["fills"] == [FILL, other]
    assert fold_fills(click, [other])["fills"] == [other]
    typed = {"kind": "fill", "locator": BOX, "param": "topic"}
    assert fold_fills(typed, [FILL]) is typed
    assert fold_fills(click, []) is click


class FormDriver:
    """A search box and a Go button. Typing stays on the page; Go moves to a results URL."""

    def __init__(self) -> None:
        self.url, self.typed = "https://site.test/", ""

    def snapshot(self) -> dict:
        elements = [] if "results" in self.url else [BOX, GO]
        return {"url": self.url, "title": "t", "elements": elements}

    def act(self, action: dict) -> None:
        if action["kind"] == "fill":
            self.typed = action["value"]
        else:
            self.url = "https://site.test/results"


def run_with_script(script: list):
    finishes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/start":
            start = {
                "state_id": 7,
                "known": False,
                "needs_model": True,
                "reason": "unknown_goal",
                "route": [],
            }
            return httpx.Response(200, json=start)
        finishes.append(body)
        return httpx.Response(200, json={"state_id": 8, "learned": 1, "goal": "g"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    atlas = Atlas("http://t", site="site.test", client=client)
    steps = iter(script)
    driver = FormDriver()
    summary = atlas.session(driver).run("Search {topic}", {"topic": "Kajol"}, fallback=lambda *_: next(steps))
    return summary, driver, finishes


def test_a_fill_then_a_click_report_as_one_step():
    typed = {"kind": "fill", "locator": BOX, "value": "Kajol"}
    summary, driver, finishes = run_with_script([typed, {"kind": "click", "locator": GO}, None])
    assert (summary.success, summary.steps, summary.fallback_calls) == (True, 2, 3)
    assert driver.typed == "Kajol"
    assert len(finishes) == 1 and len(finishes[0]["steps"]) == 1
    step = finishes[0]["steps"][0]
    assert step["action"]["kind"] == "click" and step["action"]["fills"] == [FILL]
    assert step["to"]["url"].endswith("/results")


def test_a_refilled_control_keeps_only_the_last_value():
    first = {"kind": "fill", "locator": BOX, "value": "typo"}
    again = {"kind": "fill", "locator": BOX, "value": "Kajol"}
    _, _, finishes = run_with_script([first, again, {"kind": "click", "locator": GO}, None])
    assert finishes[0]["steps"][0]["action"]["fills"] == [FILL]
