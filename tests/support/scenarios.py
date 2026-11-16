"""HTTP scenarios shared by the API unit tests (fake store) and the integration tests (HydraDB)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from atlas.contracts.api_models import LOCATE_PATH, PLAN_PATH, REPORT_PATH
from atlas.contracts.ports import GraphStore
from atlas.settings import Settings
from tests.support.builders import GOAL, SITE, FixedClock, click, page

HOME = page("/", "link:Orders")
ORDERS = page("/orders", "button:Refund", rows=2)
DONE = page("/orders/7", "link:Back")


def client_for(store: GraphStore, ns: str, api_key: str | None = None, clock: object = None) -> TestClient:
    from atlas.api.app import create_app

    settings = Settings(_env_file=None, atlas_namespace=ns, atlas_api_key=api_key)  # type: ignore[call-arg]
    return TestClient(create_app(store, clock or FixedClock(), settings))


def post(client: TestClient, path: str, body: dict) -> dict:
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def report_body(from_state: int, action, to_snapshot, **extra) -> dict:
    return {
        "site": SITE,
        "from_state": from_state,
        "action": action.model_dump(mode="json"),
        "to_snapshot": to_snapshot.model_dump(mode="json"),
        "ok": True,
        "ms": 120,
        "event_id": f"evt-{uuid.uuid4().hex}",
        **extra,
    }


def locate_plan_report_round_trip(client: TestClient) -> None:
    """Empty map -> needs_model; report two steps -> the same plan is served from the map."""
    home = post(client, LOCATE_PATH, {"site": SITE, "snapshot": HOME.model_dump(mode="json")})
    assert home["known"] is False
    plan_body = {"site": SITE, "state_id": home["state_id"], "goal_text": GOAL, "params": {}}
    before = post(client, PLAN_PATH, plan_body)
    assert (before["route"], before["needs_model"], before["reason"]) == ([], True, "unknown_goal")

    first = post(client, REPORT_PATH, report_body(home["state_id"], click("Orders", "link"), ORDERS))
    assert (first["known"], first["duplicate"]) == (False, False)
    last = report_body(first["state_id"], click("Refund"), DONE, goal_text=GOAL, goal_reached=True)
    second = post(client, REPORT_PATH, last)
    assert post(client, REPORT_PATH, last) == {**second, "known": True, "duplicate": True}

    again = post(client, LOCATE_PATH, {"site": SITE, "snapshot": HOME.model_dump(mode="json")})
    assert again == {"state_id": home["state_id"], "known": True}
    after = post(client, PLAN_PATH, plan_body)
    assert after["needs_model"] is False
    assert after["reason"] == "route_found"
    assert [step["action"]["locator"]["name"] for step in after["route"]] == ["Orders", "Refund"]
    assert [step["expect_state"] for step in after["route"]] == [first["state_id"], second["state_id"]]
    assert all(0 < step["reliability"] < 1 for step in after["route"])
    assert 0 < after["confidence"] <= 1
