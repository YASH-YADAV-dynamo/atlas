"""Route API on the in-memory store. Targets atlas.api.app.create_app(store, clock, settings)."""

from __future__ import annotations

import inspect

import pytest

from atlas.contracts import errors
from atlas.contracts.api_models import HEALTH_PATH, LOCATE_PATH, MAP_PATH, PLAN_PATH, REPORT_PATH
from tests.support.builders import GOAL, SITE, click
from tests.support.scenarios import HOME, ORDERS, client_for, locate_plan_report_round_trip, post, report_body

INJECTION = "x'}) MATCH (n) DETACH DELETE n //"
SNAPSHOT = HOME.model_dump(mode="json")
VALID_ID = 4242


def test_locate_plan_report_round_trip(fake_store, ns):
    """R-API-1, R-API-2, R-API-3: the three calls work together and match SPEC 3b."""
    locate_plan_report_round_trip(client_for(fake_store, ns))


def test_health_and_map(fake_store, ns):
    """R-API-6: /healthz answers ok; the map endpoint returns what was learned."""
    client = client_for(fake_store, ns)
    assert client.get(HEALTH_PATH).json() == {"status": "ok"}
    locate_plan_report_round_trip(client)
    view = client.get(MAP_PATH.format(site=SITE)).json()
    assert len(view["states"]) == 3
    assert len(view["transitions"]) == 2


BAD_REQUESTS = [
    (LOCATE_PATH, {"site": SITE}),
    (LOCATE_PATH, {"site": INJECTION, "snapshot": SNAPSHOT}),
    (LOCATE_PATH, {"site": SITE, "snapshot": SNAPSHOT, "unexpected": 1}),
    (LOCATE_PATH, {"site": SITE, "snapshot": {"url": "", "elements": []}}),
    (PLAN_PATH, {"site": SITE, "state_id": "1 OR 1=1", "goal_text": GOAL}),
    (PLAN_PATH, {"site": SITE, "state_id": -5, "goal_text": GOAL}),
    (PLAN_PATH, {"site": SITE, "state_id": 2**63, "goal_text": GOAL}),
    (PLAN_PATH, {"site": SITE, "state_id": VALID_ID, "goal_text": ""}),
    (PLAN_PATH, {"site": SITE, "state_id": VALID_ID, "goal_text": GOAL, "params": {INJECTION: "v"}}),
    (REPORT_PATH, report_body(VALID_ID, click("Orders", "link"), ORDERS, event_id=INJECTION)),
    (REPORT_PATH, report_body(VALID_ID, click("Orders", "link"), ORDERS, ms=-1)),
    (REPORT_PATH, report_body(VALID_ID, click("Orders", "link"), ORDERS, goal_reached=True)),
    (
        REPORT_PATH,
        {**report_body(VALID_ID, click("Orders", "link"), ORDERS), "action": {"kind": "eval", "value": "x"}},
    ),
]


@pytest.mark.parametrize(("path", "body"), BAD_REQUESTS)
def test_invalid_input_is_rejected_before_it_reaches_the_map(fake_store, ns, path, body):
    """R-API-4, R-SEC-2: malformed or hostile fields get 422 and nothing is written."""
    response = client_for(fake_store, ns).post(path, json=body)
    assert response.status_code == 422
    view = fake_store.get_map(ns, SITE)
    assert (view.states, view.transitions) == ((), ())


def test_free_text_injection_is_harmless(fake_store, ns):
    """R-SEC-2: goal text is free text; a Cypher payload in it is just an unknown goal."""
    client = client_for(fake_store, ns)
    locate_plan_report_round_trip(client)
    before = fake_store.get_map(ns, SITE)
    home = post(client, LOCATE_PATH, {"site": SITE, "snapshot": SNAPSHOT})
    hostile = post(client, PLAN_PATH, {"site": SITE, "state_id": home["state_id"], "goal_text": INJECTION})
    assert (hostile["route"], hostile["needs_model"]) == ([], True)
    assert fake_store.get_map(ns, SITE) == before


def test_typed_errors_use_the_single_mapping(fake_store, ns):
    """R-API-5: an AtlasError becomes its mapped status with the {code, message} envelope."""
    response = client_for(fake_store, ns).post(
        PLAN_PATH, json={"site": SITE, "state_id": VALID_ID, "goal_text": GOAL}
    )
    assert response.status_code == errors.HTTP_STATUS[errors.UnknownStateError] == 404
    assert response.json()["code"] == "unknown_state"
    assert set(response.json()) == {"code", "message"}


def test_every_typed_error_has_a_status_and_a_code():
    """R-API-5: no AtlasError subclass is missing from the mapping table."""
    kinds = [
        kind
        for _, kind in inspect.getmembers(errors, inspect.isclass)
        if issubclass(kind, errors.AtlasError) and kind is not errors.AtlasError
    ]
    assert kinds
    for kind in kinds:
        assert kind in errors.HTTP_STATUS, kind.__name__
        assert errors.http_status_for(kind("x")) == errors.HTTP_STATUS[kind]
        assert kind.code != "internal"
    assert len({kind.code for kind in kinds}) == len(kinds)
    assert errors.http_status_for(errors.AtlasError("x")) == 500


def test_api_key_is_enforced_when_configured(fake_store, ns):
    """R-API-7: with ATLAS_API_KEY set, /v1/* needs the bearer key; /healthz stays open."""
    client = client_for(fake_store, ns, api_key="test-key-not-a-secret")
    body = {"site": SITE, "snapshot": SNAPSHOT}
    assert client.get(HEALTH_PATH).status_code == 200
    assert client.post(LOCATE_PATH, json=body).status_code == 401
    assert client.post(LOCATE_PATH, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (
        client.post(
            LOCATE_PATH, json=body, headers={"Authorization": "Bearer test-key-not-a-secret"}
        ).status_code
        == 200
    )
