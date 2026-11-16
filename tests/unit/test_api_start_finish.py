"""`POST /v1/start` and `POST /v1/finish` (SHIP_PLAN 3.4) on the in-memory store."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas.contracts.api_models import FINISH_PATH, LOCATE_PATH, PLAN_PATH, REPORT_PATH, START_PATH
from tests.support.hosted import bearer, hosted, hosted_settings, new_key
from tests.support.scenarios import client_for

SITE = "wiki.test"
GOAL = "Open the article about {topic}"
HOME = {
    "url": f"https://{SITE}/",
    "title": "Home",
    "elements": [{"role": "searchbox", "name": "Search"}, {"role": "button", "name": "Go"}],
}


def article(topic: str) -> dict:
    return {
        "url": f"https://{SITE}/wiki/{topic.replace(' ', '_')}",
        "title": f"{topic} - Wiki",
        "elements": [{"role": "link", "name": "Edit"}, {"role": "link", "name": "Talk"}],
    }


def click(name: str, role: str = "button") -> dict:
    return {"kind": "click", "locator": {"role": role, "name": name}}


def start_body(topic: str = "Shah Rukh Khan", **extra) -> dict:
    return {"site": SITE, "snapshot": HOME, "goal_text": GOAL, "params": {"topic": topic}, **extra}


def learn(client: TestClient, topic: str = "Shah Rukh Khan", **extra) -> dict:
    started = client.post(START_PATH, json=start_body(topic)).json()
    body = {
        "site": SITE,
        "from_state": started["state_id"],
        "steps": [
            {"action": click("Go"), "to": {"url": f"https://{SITE}/w?search=x", "elements": []}},
            {"action": click(topic, "link"), "to": article(topic)},
        ],
        "ok": True,
        "goal_text": GOAL,
        "params": {"topic": topic},
        "goal_reached": True,
        "kind": "read",
        **extra,
    }
    response = client.post(FINISH_PATH, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_start_on_an_empty_map(fake_store, ns):
    out = client_for(fake_store, ns).post(START_PATH, json=start_body()).json()
    assert (out["needs_model"], out["reason"], out["route"], out["known"]) == (
        True,
        "unknown_goal",
        [],
        False,
    )
    assert "plan_id" not in out
    assert "open_url" not in out
    assert isinstance(out["state_id"], int)


def test_a_round_trip_serves_the_route_and_the_address(fake_store, ns):
    client = client_for(fake_store, ns)
    done = learn(client)
    assert done["learned"] == 2
    assert done["goal"] == "open the article about {topic}"
    same = client.post(START_PATH, json=start_body()).json()
    assert same["open_url"] == f"https://{SITE}/wiki/Shah_Rukh_Khan"
    assert (same["match"], same["needs_model"], same["reason"]) == ("exact", False, "route_found")
    new = client.post(START_PATH, json=start_body("Hrithik Roshan")).json()
    assert new["open_url"] == f"https://{SITE}/wiki/Hrithik_Roshan"
    assert len(new["route"]) == 2
    assert len(new["plan_id"]) == 12


def test_finish_credits_an_issued_plan(fake_store, ns):
    client = client_for(fake_store, ns)
    learn(client)
    planned = client.post(START_PATH, json=start_body("Hrithik Roshan")).json()
    body = {
        "site": SITE,
        "plan_id": planned["plan_id"],
        "done": 2,
        "ok": True,
        "to_snapshot": article("Hrithik Roshan"),
        "params": {"topic": "Hrithik Roshan"},
    }
    out = client.post(FINISH_PATH, json=body).json()
    assert out["learned"] == 2
    assert out["state_id"] == planned["route"][-1]["expect_state"]


def test_start_and_the_old_calls_agree_on_the_page_and_the_plan(fake_store, ns):
    client = client_for(fake_store, ns)
    learn(client)
    located = client.post(
        LOCATE_PATH, json={"site": SITE, "snapshot": HOME, "params": {"topic": "Hrithik Roshan"}}
    )
    started = client.post(START_PATH, json=start_body("Hrithik Roshan")).json()
    assert started["state_id"] == located.json()["state_id"]
    plan = client.post(
        PLAN_PATH,
        json={
            "site": SITE,
            "state_id": started["state_id"],
            "goal_text": GOAL,
            "params": {"topic": "Hrithik Roshan"},
        },
    ).json()
    assert plan["route"] == started["route"]


def test_a_blocked_start_and_finish_learn_nothing(fake_store, ns):
    client = client_for(fake_store, ns)
    wall = {"url": f"https://{SITE}/captcha", "title": "Just a moment"}
    out = client.post(START_PATH, json={**start_body(), "snapshot": wall}).json()
    assert out["blocked"] == "url_path:captcha"
    fin = client.post(
        FINISH_PATH,
        json={"site": SITE, "ok": True, "to_snapshot": wall, "goal_text": GOAL, "goal_reached": True},
    ).json()
    assert (fin["blocked"], fin["learned"]) == ("url_path:captcha", 0)
    assert fake_store.get_map(ns, SITE).states == ()


def test_an_oversize_snapshot_is_accepted(fake_store, ns):
    big = {**HOME, "elements": [{"role": "link", "name": f"item {i}"} for i in range(600)]}
    response = client_for(fake_store, ns).post(START_PATH, json={**start_body(), "snapshot": big})
    assert response.status_code == 200


BAD = [
    (START_PATH, {"site": SITE, "snapshot": HOME}),
    (START_PATH, {**start_body(), "replay": "eval"}),
    (START_PATH, {**start_body(), "unexpected": 1}),
    (START_PATH, {**start_body(), "site": "x'}) MATCH (n) DETACH DELETE n //"}),
    (FINISH_PATH, {"site": SITE, "ok": True}),
    (FINISH_PATH, {"site": SITE, "ok": True, "done": 1, "to_snapshot": HOME}),
    (FINISH_PATH, {"site": SITE, "ok": True, "to_snapshot": HOME, "goal_reached": True}),
    (FINISH_PATH, {"site": SITE, "ok": True, "steps": [{"action": click("Go"), "to": HOME}]}),
    (FINISH_PATH, {"site": SITE, "ok": True, "plan_id": "short"}),
    (
        FINISH_PATH,
        {
            "site": SITE,
            "ok": True,
            "to_snapshot": HOME,
            "steps": [{"action": click("Go"), "to": HOME}] * 13,
            "from_state": 5,
        },
    ),
]


@pytest.mark.parametrize(("path", "body"), BAD)
def test_invalid_input_is_rejected_and_nothing_is_written(fake_store, ns, path, body):
    assert client_for(fake_store, ns).post(path, json=body).status_code == 422
    assert fake_store.get_map(ns, SITE).states == ()


def test_an_unknown_plan_alone_is_a_404(fake_store, ns):
    response = client_for(fake_store, ns).post(
        FINISH_PATH, json={"site": SITE, "ok": True, "plan_id": "a" * 12}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "unknown_plan"


def test_keys_isolate_namespaces(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        assert client.post(START_PATH, json=start_body()).status_code == 401
        assert (
            client.post(FINISH_PATH, json={"site": SITE, "ok": True, "to_snapshot": HOME}).status_code == 401
        )
        mine = bearer(new_key(client, "1.2.3.4"))
        theirs = bearer(new_key(client, "1.2.3.5"))
        body = {
            "site": SITE,
            "ok": True,
            "to_snapshot": article("Shah Rukh Khan"),
            "goal_text": GOAL,
            "params": {"topic": "Shah Rukh Khan"},
            "goal_reached": True,
            "kind": "read",
        }
        assert client.post(FINISH_PATH, json=body, headers=mine).status_code == 200
        assert client.post(START_PATH, json=start_body(), headers=mine).json()["open_url"]
        assert "open_url" not in client.post(START_PATH, json=start_body(), headers=theirs).json()


def test_report_still_works_beside_finish(fake_store, ns):
    client = client_for(fake_store, ns)
    started = client.post(START_PATH, json=start_body()).json()
    body = {
        "site": SITE,
        "from_state": started["state_id"],
        "action": click("Go"),
        "to_snapshot": article("Shah Rukh Khan"),
        "ok": True,
        "ms": 5,
        "event_id": "evt-00000001",
    }
    assert client.post(REPORT_PATH, json=body).status_code == 200
