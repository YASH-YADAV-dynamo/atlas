"""Hosted MCP tenancy: every bearer key is a principal bound to one namespace, carried per
request. Runs on the in-memory store and on real HydraDB (`any_store`, skipped without
HYDRADB_URL). Targets atlas.mcp.keys, atlas.mcp.server.TenantGate and atlas.api.app."""

from __future__ import annotations

import json

from atlas.mcp.keys import KeyRegistry
from tests.support.builders import SITE
from tests.support.hosted import (
    ADMIN,
    DEMO,
    START,
    RawMcp,
    bearer,
    hosted,
    hosted_settings,
    new_key,
    plan_args,
    report_args,
    snapshot_json,
)


def learn_route(mcp: RawMcp) -> int:
    """The tenant reports one step (Order 1042 opened) and the goal; returns the start state."""
    error, planned = mcp.call("atlas_plan", plan_args())
    assert not error and planned["needs_model"], planned
    error, reported = mcp.call("atlas_report", report_args(planned["state_id"]))
    assert not error, reported
    return planned["state_id"]


def map_size(client, key: str) -> int:
    response = client.get(f"/v1/map/{SITE}", headers=bearer(key))
    assert response.status_code == 200, response.text
    return len(response.json()["states"])


def test_tenant_a_cannot_read_or_write_tenant_b_via_mcp_or_rest(any_store, ns, tmp_path):
    with hosted(any_store, hosted_settings(tmp_path, ns)) as client:
        key_a, key_b = new_key(client, "10.0.0.1"), new_key(client, "10.0.0.2")
        a, b = RawMcp(client, key_a).initialize(), RawMcp(client, key_b).initialize()
        start = learn_route(a)

        # A's map serves A, over MCP and over REST with the same key.
        error, planned = a.call("atlas_plan", plan_args())
        assert not error and not planned["needs_model"] and len(planned["route"]) == 1
        rest = client.post(
            "/v1/plan",
            json={
                "site": SITE,
                "state_id": start,
                "goal_text": "Open order 1042",
                "params": {"order_id": "1042"},
            },
            headers=bearer(key_a),
        )
        assert rest.status_code == 200 and rest.json()["route"], rest.text
        error, found = a.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and found["found"]

        # B sees none of it: no route, no entity, no site, an empty map, no skills.
        error, planned_b = b.call("atlas_plan", plan_args())
        assert not error and planned_b["needs_model"] and "route" not in planned_b
        error, found_b = b.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and not found_b["found"]
        error, skills_b = b.call("atlas_find_skills", {"site": SITE, "goal_text": "open order"})
        assert not error and not skills_b.get("skills")
        assert b.read("atlas://sites")["sites"] == [{"site": SITE, "states": 1, "transitions": 0}]
        assert (
            client.post(
                "/v1/plan",
                json={
                    "site": SITE,
                    "state_id": start,
                    "goal_text": "Open order 1042",
                    "params": {"order_id": "1042"},
                },
                headers=bearer(key_b),
            ).status_code
            == 404
        )  # A's state id is unknown in B's namespace
        sites_a = a.read("atlas://sites")["sites"]
        assert sites_a == [{"site": SITE, "states": 2, "transitions": 1}]

        # B writes with A's state id: it lands in B's namespace only, A's map is unchanged.
        before = client.get(f"/v1/map/{SITE}", headers=bearer(key_a)).json()
        b.call("atlas_report", report_args(start, event_id="evt-from-b"))  # refused or kept in B
        assert not error
        assert client.get(f"/v1/map/{SITE}", headers=bearer(key_a)).json() == before
        # The admin namespace (demo) never saw either tenant.
        assert map_size(client, ADMIN) == 0


def test_request_cannot_name_another_namespace(fake_store, ns, tmp_path):
    """Inputs have no namespace field; an extra one is rejected, never honoured."""
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        mcp = RawMcp(client, new_key(client, "10.0.0.3")).initialize()
        error, message = mcp.call("atlas_plan", {**plan_args(), "ns": ns, "namespace": ns})
        assert error and "invalid" in message
        body = {"site": SITE, "snapshot": snapshot_json(START), "ns": ns}
        assert (
            client.post("/v1/locate", json=body, headers=bearer(new_key(client, "10.0.0.4"))).status_code
            == 422
        )
        assert map_size(client, ADMIN) == 0


def test_demo_key_is_read_only(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        admin = RawMcp(client, ADMIN).initialize()
        start = learn_route(admin)  # the seeded demo namespace
        size = map_size(client, ADMIN)
        demo = RawMcp(client, DEMO).initialize()

        error, planned = demo.call("atlas_plan", plan_args())
        assert not error and planned["state_id"] == start and planned["route"]
        error, skills = demo.call("atlas_find_skills", {"site": SITE, "goal_text": "open order"})
        assert not error and skills["skills"]
        error, found = demo.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and found["found"]

        error, message = demo.call("atlas_report", report_args(start, event_id="evt-demo"))
        assert error and "read-only" in message
        # Planning from a page the map never saw records nothing for a read-only key.
        unknown = {**plan_args(), "snapshot_summary": {"url": f"https://{SITE}/new", "title": "new"}}
        error, planned = demo.call("atlas_plan", unknown)
        assert not error and planned["needs_model"]
        assert map_size(client, ADMIN) == size

        rest = client.post(
            "/v1/report",
            json={
                "site": SITE,
                "from_state": start,
                "action": json.loads(json.dumps(report_args(start)["action"])),
                "to_snapshot": report_args(start)["to_snapshot_summary"],
                "ok": True,
                "ms": 1,
                "event_id": "evt-demo-rest",
            },
            headers=bearer(DEMO),
        )
        assert rest.status_code == 403 and rest.json()["code"] == "read_only_key"
        assert client.post(f"/v1/sleep?site={SITE}", headers=bearer(DEMO)).status_code == 403
        locate = client.post(
            "/v1/locate", json={"site": SITE, "snapshot": unknown["snapshot_summary"]}, headers=bearer(DEMO)
        )
        assert locate.status_code == 200 and locate.json()["known"] is False
        assert map_size(client, ADMIN) == size


def test_interleaved_sessions_never_mix(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        a = RawMcp(client, new_key(client, "10.0.1.1")).initialize()
        b = RawMcp(client, new_key(client, "10.0.1.2")).initialize()
        plan_a = a.call("atlas_plan", plan_args("1042"))[1]
        plan_b = b.call("atlas_plan", plan_args("2001"))[1]
        assert not a.call("atlas_report", report_args(plan_a["state_id"], "1042"))[0]
        assert not b.call("atlas_report", report_args(plan_b["state_id"], "2001"))[0]
        for _ in range(3):
            assert a.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})[1]["found"]
            assert not b.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})[1]["found"]
            assert b.call("atlas_recall", {"entity_type": "order_id", "key": "2001"})[1]["found"]
            assert not a.call("atlas_recall", {"entity_type": "order_id", "key": "2001"})[1]["found"]


def test_a_session_id_is_bound_to_the_key_that_created_it(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        owner = RawMcp(client, new_key(client, "10.0.2.1")).initialize()
        learn_route(owner)
        thief = RawMcp(client, new_key(client, "10.0.2.2"))
        thief.session_id = owner.session_id
        status, _ = thief.request(
            "tools/call", {"name": "atlas_recall", "arguments": {"entity_type": "order_id", "key": "1042"}}
        )
        assert status == 404
        demo = RawMcp(client, DEMO)
        demo.session_id = owner.session_id
        assert demo.request("tools/list", {})[0] == 404
        anonymous = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
            headers={"mcp-session-id": owner.session_id or ""},
        )
        assert anonymous.status_code == 401
        made_up = RawMcp(client, owner.key)
        made_up.session_id = "not-a-session"
        assert made_up.request("tools/list", {})[0] == 404
        # The owner keeps working.
        assert owner.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})[1]["found"]


def test_keys_are_stored_hashed_only(fake_store, ns, tmp_path):
    settings = hosted_settings(tmp_path, ns)
    with hosted(fake_store, settings) as client:
        response = client.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.7"})
        body = response.json()
    stored = (tmp_path / "keys.json").read_text(encoding="utf-8")
    assert body["api_key"].startswith("atl_") and body["api_key"] not in stored
    assert "203.0.113.7" not in stored and ADMIN not in stored and DEMO not in stored
    record = json.loads(stored)["tenants"][0]
    assert record["namespace"] == body["namespace"] and len(record["key_hash"]) == 64
    # Durable: a new process with the same pepper resolves the key; another pepper does not.
    principal = KeyRegistry(settings).authenticate(body["api_key"])
    assert principal is not None and principal.namespace == body["namespace"] and not principal.read_only
    other = hosted_settings(tmp_path, ns, atlas_key_pepper="another-pepper")
    assert KeyRegistry(other).authenticate(body["api_key"]) is None


def test_key_response_shape(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        body = client.post("/v1/keys").json()
    assert set(body) == {"api_key", "namespace", "mcp_url", "read_only"}
    assert body["namespace"].startswith("t_") and body["namespace"] != ns
    assert body["mcp_url"] == "https://atlas.test/mcp" and body["read_only"] is False


def test_per_ip_creation_cap_uses_the_last_forwarded_entry(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, tenant_keys_per_ip_day=3)) as client:
        for spoof in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            assert (
                client.post("/v1/keys", headers={"X-Forwarded-For": f"{spoof}, 198.51.100.9"}).status_code
                == 201
            )
        refused = client.post("/v1/keys", headers={"X-Forwarded-For": "4.4.4.4, 198.51.100.9"})
        assert refused.status_code == 429 and refused.json()["code"] == "tenant_limit"
        assert client.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.10"}).status_code == 201


def test_global_tenant_cap(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, tenant_max=2)) as client:
        new_key(client, "10.1.0.1")
        new_key(client, "10.1.0.2")
        assert client.post("/v1/keys", headers={"X-Forwarded-For": "10.1.0.3"}).status_code == 429


def test_keys_need_a_pepper(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, atlas_key_pepper=None)) as client:
        response = client.post("/v1/keys")
        assert response.status_code == 503 and response.json()["code"] == "keys_disabled"


def test_per_tenant_event_cap(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, tenant_max_events=1)) as client:
        mcp = RawMcp(client, new_key(client, "10.2.0.1")).initialize()
        start = learn_route(mcp)
        # A retry of a stored event is a duplicate, not a new event.
        error, message = mcp.call("atlas_report", report_args(start, event_id="evt-second"))
        assert error and "limit" in message


def test_per_key_rate_limit(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, tenant_requests_per_minute=3)) as client:
        key = new_key(client, "10.3.0.1")
        statuses = [client.get(f"/v1/map/{SITE}", headers=bearer(key)).status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        # The admin key is never limited.
        assert all(client.get(f"/v1/map/{SITE}", headers=bearer(ADMIN)).status_code == 200 for _ in range(5))


def test_unknown_key_is_refused_everywhere(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        assert client.get(f"/v1/map/{SITE}", headers=bearer("atl_made_up")).status_code == 401
        assert client.get(f"/v1/map/{SITE}").status_code == 401
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp", json=body, headers=bearer("atl_made_up")).status_code == 401


def test_mcp_info_shape(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        body = client.get("/v1/mcp-info").json()
    assert body == {
        "mcp_url": "https://atlas.test/mcp",
        "demo_key": DEMO,
        "tools": ["atlas_find_skills", "atlas_recall", "atlas_plan", "atlas_report"],
    }


def test_mcp_info_derives_the_url_from_the_proxy(fake_store, ns, tmp_path):
    settings = hosted_settings(tmp_path, ns, atlas_public_url=None, atlas_demo_mcp_key=None)
    with hosted(fake_store, settings) as client:
        body = client.get(
            "/v1/mcp-info", headers={"X-Forwarded-Proto": "https", "Host": "example.org"}
        ).json()
    assert body["mcp_url"] == "https://example.org/mcp" and body["demo_key"] is None
