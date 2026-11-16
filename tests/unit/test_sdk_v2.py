"""SDK 0.2.0 (SHIP_PLAN 3.5): one /v1/start and one /v1/finish per run, re-plans only at the
frontier, an answer page opened directly (`open_url`), `kind`, and the new RunSummary fields."""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("atlas_sdk")
pytest.importorskip("atlas.api.app", reason="needs the Route API")

from atlas_sdk import Atlas, RunSummary

from tests.support.builders import SITE
from tests.support.fake_site import FakeSite
from tests.support.scenarios import client_for
from tests.unit.test_sdk import AgentStep, DictDriver

WIKI = "en.wikipedia.org"
MAIN = f"https://{WIKI}/wiki/Main_Page"
BOX = {"role": "searchbox", "name": "Search Wikipedia"}


class Spy:
    """Wraps an httpx-like client and records every request path."""

    def __init__(self, client) -> None:
        self.client, self.paths = client, []

    def post(self, url: str, **kw):
        self.paths.append(url.removeprefix("http://testserver"))
        return self.client.post(url, **kw)


def test_one_start_and_one_finish_per_run(fake_store, ns):
    spy = Spy(client_for(fake_store, ns))
    atlas = Atlas("http://testserver", site=SITE, client=spy)  # type: ignore[arg-type]
    agent = AgentStep()
    first = atlas.session(DictDriver(FakeSite())).run("Refund order 1", {"order_id": "1"}, fallback=agent)
    assert first.success and first.via == "explore" and first.fallback_calls > 0
    assert spy.paths == ["/v1/start", "/v1/finish"]
    spy.paths.clear()
    second = atlas.session(DictDriver(FakeSite())).run("Refund order 2", {"order_id": "2"}, fallback=agent)
    assert (second.success, second.via, second.map_steps, second.fallback_calls) == (True, "route", 5, 0)
    assert second.reason == "route_found" and isinstance(second.state_id, int)
    assert spy.paths == ["/v1/start", "/v1/finish"]


def test_summary_extras_do_not_affect_equality():
    assert RunSummary(True, 1, 1, 0, "route", "route_found", 3) == RunSummary(True, 1, 1, 0)


class WikiDriver:
    def __init__(self) -> None:
        self.url, self.title, self.acts = MAIN, "Main Page", []

    def snapshot(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "elements": [{"role": "searchbox", "name": BOX["name"]}],
        }

    def act(self, action: dict) -> None:
        self.acts.append(action["kind"])
        if action["kind"] == "goto":
            self.url = action["value"]
        else:
            self.url = f"https://{WIKI}/wiki/{action['fills'][0]['value'].replace(' ', '_')}"
        self.title = self.url.rsplit("/", 1)[1].replace("_", " ") + " - Wikipedia"


def test_a_read_goal_is_remembered_and_opened_directly(fake_store, ns):
    atlas = Atlas("http://testserver", site=WIKI, client=client_for(fake_store, ns))
    goal, calls = "Open the Wikipedia article about {topic}", []

    def agent(snap: dict, _goal: str, params: dict) -> dict | None:
        calls.append(snap["url"])
        if snap["url"] != MAIN:
            return None
        fill = {"locator": BOX, "value": params["topic"]}
        return {"kind": "press", "locator": BOX, "value": "Enter", "fills": [fill]}

    first = atlas.session(WikiDriver()).run(goal, {"topic": "Kajol"}, fallback=agent, kind="read")
    assert (first.success, first.via, len(calls)) == (True, "explore", 2)
    calls.clear()
    driver = WikiDriver()
    again = atlas.session(driver).run(goal, {"topic": "Kajol"}, fallback=agent, kind="read")
    assert (again.success, again.via, again.steps, again.map_steps, calls) == (True, "url", 1, 0, [])
    assert driver.acts == ["goto"] and driver.url.endswith("/wiki/Kajol")


class Scripted:
    """A fake HTTP server answering /v1/start from a list and recording every body."""

    def __init__(self, starts: list[dict]) -> None:
        self.starts, self.bodies = list(starts), []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append((request.url.path, body))
        if request.url.path == "/v1/start":
            return httpx.Response(200, json=self.starts.pop(0))
        return httpx.Response(200, json={"state_id": 2, "learned": 1, "goal": "g"})


def served(route: list[dict], **extra) -> dict:
    base = {"state_id": 1, "known": True, "needs_model": False, "reason": "route_found"}
    return {**base, "plan_id": "abcdefghij12", "route": route, **extra}


def unknown() -> dict:
    return {"state_id": 5, "known": False, "needs_model": True, "reason": "unknown_goal", "route": []}


def hop(name: str) -> dict:
    return {"action": {"kind": "click", "locator": {"role": "link", "name": name}}, "expect_state": 9}


class FlakyDriver:
    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on, self.seen = fail_on, []

    def snapshot(self) -> dict:
        return {"url": "https://x.test/", "title": "t", "elements": []}

    def act(self, action: dict) -> None:
        self.seen.append((action.get("locator") or {}).get("name"))
        if self.fail_on is not None and self.seen[-1] == self.fail_on:
            raise RuntimeError("control not found")


def test_a_failed_route_step_reports_the_prefix_and_replans():
    api = Scripted([served([hop("A"), hop("B")]), unknown()])
    atlas = Atlas("http://t", site="x.test", client=api.client())
    summary = atlas.session(FlakyDriver(fail_on="B")).run("Go {x}", {"x": "1"})
    assert (summary.success, summary.steps, summary.map_steps) == (False, 2, 2)
    assert [path for path, _ in api.bodies] == ["/v1/start", "/v1/finish", "/v1/start"]
    finish = api.bodies[1][1]
    assert (finish["plan_id"], finish["done"], finish["ok"], finish["goal_reached"]) == (
        "abcdefghij12",
        1,
        False,
        False,
    )


def test_open_url_navigates_and_credits_no_route_steps():
    api = Scripted([served([hop("A")], open_url="https://x.test/answer")])
    driver = FlakyDriver()
    atlas = Atlas("http://t", site="x.test", client=api.client())
    summary = atlas.session(driver).run("Read {x}", {"x": "1"}, kind="read")
    assert (summary.success, summary.via, summary.steps, summary.map_steps) == (True, "url", 1, 0)
    assert driver.seen == [None]
    finish = api.bodies[1][1]
    assert (finish["done"], finish["goal_reached"], finish["kind"]) == (0, True, "read")


def test_a_long_exploration_is_sent_in_batches_of_twelve_steps():
    api = Scripted([unknown()])
    clicks = iter([{"kind": "click", "locator": {"role": "link", "name": f"L{i}"}} for i in range(14)])
    atlas = Atlas("http://t", site="x.test", client=api.client())
    summary = atlas.session(FlakyDriver()).run("Go", fallback=lambda *_: next(clicks, None), max_steps=30)
    sizes = [len(body["steps"]) for path, body in api.bodies if path == "/v1/finish"]
    assert (summary.success, summary.steps, sizes) == (True, 14, [12, 2])
