"""`/mcp/k/<key>` behaves exactly like `/mcp` with `Authorization: Bearer <key>`: same principal,
tenancy, session binding, read-only demo key. Runs on the in-memory store and on real HydraDB
(`any_store`). Targets atlas.mcp.server.TenantGate, atlas.mcp.logscrub and atlas.api.app."""

from __future__ import annotations

import logging
from typing import Any

from atlas.mcp.logscrub import install_access_log_scrub
from tests.support.hosted import (
    ACCEPT,
    DEMO,
    PROTOCOL,
    RawMcp,
    _messages,
    hosted,
    hosted_settings,
    new_key,
    plan_args,
    report_args,
)


class PathMcp(RawMcp):
    """RawMcp whose key travels in the URL path instead of the Authorization header."""

    def url(self) -> str:
        return f"/mcp/k/{self.key}"

    def headers(self) -> dict[str, str]:
        headers = {**ACCEPT, "mcp-protocol-version": PROTOCOL}
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id
        return headers

    def initialize(self) -> PathMcp:
        body = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "path-test", "version": "0"},
            },
        }
        response = self.client.post(self.url(), json=body, headers=ACCEPT)
        assert response.status_code == 200, response.text
        self.session_id = response.headers["mcp-session-id"]
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert self.client.post(self.url(), json=note, headers=self.headers()).status_code == 202
        return self

    def request(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        self._next += 1
        body = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        response = self.client.post(self.url(), json=body, headers=self.headers())
        if response.status_code != 200:
            return response.status_code, None
        return 200, next(m for m in _messages(response.text) if m.get("id") == self._next)


def learn(mcp: RawMcp) -> int:
    error, planned = mcp.call("atlas_plan", plan_args())
    assert not error, planned
    error, reported = mcp.call("atlas_report", report_args(planned["state_id"]))
    assert not error, reported
    return planned["state_id"]


def test_tenants_stay_isolated_over_the_path_route(any_store, ns, tmp_path):
    with hosted(any_store, hosted_settings(tmp_path, ns)) as client:
        a = PathMcp(client, new_key(client, "10.0.0.1")).initialize()
        b = PathMcp(client, new_key(client, "10.0.0.2")).initialize()
        learn(a)
        error, found = a.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and found["found"]
        error, other = b.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and not other["found"]
        # The header route of the same key sees the same map (same principal).
        header_view = RawMcp(client, a.key).initialize()
        error, again = header_view.call("atlas_recall", {"entity_type": "order_id", "key": "1042"})
        assert not error and again["found"]


def test_a_session_id_stolen_across_path_keys_is_rejected(any_store, ns, tmp_path):
    with hosted(any_store, hosted_settings(tmp_path, ns)) as client:
        a = PathMcp(client, new_key(client, "10.0.0.1")).initialize()
        thief = PathMcp(client, new_key(client, "10.0.0.2"))
        thief.session_id = a.session_id
        status, _ = thief.request("tools/list", {})
        assert status == 404
        header_thief = RawMcp(client, thief.key)
        header_thief.session_id = a.session_id
        assert header_thief.request("tools/list", {})[0] == 404
        assert a.request("tools/list", {})[0] == 200


def test_wrong_path_key_is_401_and_not_echoed(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        secret = "atl_wrong-key-should-not-appear"
        response = client.post(f"/mcp/k/{secret}", json={}, headers=ACCEPT)
        assert response.status_code == 401
        assert secret not in response.text and secret not in str(response.headers)
        assert client.post("/mcp/k/", json={}, headers=ACCEPT).status_code == 401


def test_demo_key_is_read_only_over_the_path_route(any_store, ns, tmp_path):
    with hosted(any_store, hosted_settings(tmp_path, ns)) as client:
        demo = PathMcp(client, DEMO).initialize()
        error, text = demo.call("atlas_report", report_args(1))
        assert error and "read-only" in text


def test_path_rate_limit_matches_the_header_route(fake_store, ns, tmp_path):
    settings = hosted_settings(tmp_path, ns, tenant_requests_per_minute=3)
    with hosted(fake_store, settings) as client:
        key = new_key(client, "10.0.0.3")
        codes = [client.post(f"/mcp/k/{key}", json={}, headers=ACCEPT).status_code for _ in range(5)]
        assert 429 in codes


def test_keys_in_the_path_never_reach_logs(caplog):
    install_access_log_scrub()
    key = "atl_secret-should-be-masked"
    logger = logging.getLogger("uvicorn.access")
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:1", "POST", f"/mcp/k/{key}?x=1", "1.1", 200)
    logger.info("fetched %s", f"/mcp/k/{key}")
    logger.info(f"fetched /mcp/k/{key}")
    assert key not in caplog.text
    assert "/mcp/k/***" in caplog.text
