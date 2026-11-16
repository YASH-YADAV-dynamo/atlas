"""The hosted app (Route API + MCP at /mcp, one namespace per key) for tenancy tests, and a
minimal raw JSON-RPC MCP client over Starlette's TestClient."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from atlas.api.app import create_app, mount_mcp
from atlas.contracts.ports import MapStore
from atlas.contracts.types import Action, ActionKind, Locator, Snapshot
from atlas.mcp.keys import KeyRegistry
from atlas.settings import Settings
from tests.support.builders import SITE, FixedClock, page

ADMIN = "admin-key-not-a-secret"
DEMO = "demo-key-not-a-secret"
PEPPER = "pepper-not-a-secret"
GOAL = "Open order {order_id}"
PROTOCOL = "2025-06-18"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

START: Snapshot = page("/orders", "link:Order 1042", "button:Search")
TARGET: Snapshot = page("/orders/1042", "button:Refund")


def open_order(order: str = "1042") -> Action:
    return Action(kind=ActionKind.CLICK, locator=Locator(role="link", name=f"Order {order}"))


def hosted_settings(tmp_path: Path, ns: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "atlas_namespace": ns,
        "atlas_api_key": ADMIN,
        "atlas_demo_mcp_key": DEMO,
        "atlas_key_pepper": PEPPER,
        "atlas_keys_path": str(tmp_path / "keys.json"),
        "atlas_public_url": "https://atlas.test",
        "mcp_site": SITE,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@contextmanager
def hosted(store: MapStore, settings: Settings) -> Iterator[TestClient]:
    keys = KeyRegistry(settings)
    app = create_app(store, FixedClock(), settings, keys)
    mount_mcp(app, store, FixedClock(), settings, keys)
    with TestClient(app) as client:
        yield client


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def new_key(client: TestClient, ip: str) -> str:
    response = client.post("/v1/keys", headers={"X-Forwarded-For": ip})
    assert response.status_code == 201, response.text
    return response.json()["api_key"]


def _messages(text: str) -> list[dict[str, Any]]:
    """JSON-RPC messages from a JSON or SSE response body."""
    text = text.strip()
    if text.startswith("{"):
        return [json.loads(text)]
    return [
        json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:") and line[5:].strip()
    ]


class RawMcp:
    """One MCP session (protocol 2025-06-18, session id header) driven with plain HTTP."""

    def __init__(self, client: TestClient, key: str) -> None:
        self.client = client
        self.key = key
        self.session_id: str | None = None
        self._next = 1

    def headers(self) -> dict[str, str]:
        headers = {**ACCEPT, **bearer(self.key), "mcp-protocol-version": PROTOCOL}
        if self.session_id is not None:
            headers["mcp-session-id"] = self.session_id
        return headers

    def initialize(self) -> RawMcp:
        body = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "tenancy-test", "version": "0"},
            },
        }
        response = self.client.post("/mcp", json=body, headers={**ACCEPT, **bearer(self.key)})
        assert response.status_code == 200, response.text
        self.session_id = response.headers["mcp-session-id"]
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        assert self.client.post("/mcp", json=note, headers=self.headers()).status_code == 202
        return self

    def request(self, method: str, params: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        self._next += 1
        body = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        response = self.client.post("/mcp", json=body, headers=self.headers())
        if response.status_code != 200:
            return response.status_code, None
        replies = [m for m in _messages(response.text) if m.get("id") == self._next]
        return 200, replies[0]

    def call(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        """(is_error, parsed JSON result or the error text)."""
        status, reply = self.request("tools/call", {"name": tool, "arguments": arguments})
        assert status == 200 and reply is not None, status
        result = reply["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return True, text
        return False, json.loads(text)

    def read(self, uri: str) -> Any:
        status, reply = self.request("resources/read", {"uri": uri})
        assert status == 200 and reply is not None
        return json.loads(reply["result"]["contents"][0]["text"])


def snapshot_json(snapshot: Snapshot) -> dict[str, Any]:
    return snapshot.model_dump(mode="json")


def report_args(from_state: int, order: str = "1042", event_id: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "site": SITE,
        "from_state": from_state,
        "action": open_order(order).model_dump(mode="json", exclude_none=True),
        "to_snapshot_summary": snapshot_json(TARGET),
        "ok": True,
        "goal_text": GOAL.replace("{order_id}", order),
        "params": {"order_id": order},
        "goal_reached": True,
    }
    if event_id is not None:
        args["event_id"] = event_id
    return args


def plan_args(order: str = "1042") -> dict[str, Any]:
    return {
        "site": SITE,
        "snapshot_summary": snapshot_json(START),
        "goal_text": GOAL.replace("{order_id}", order),
        "params": {"order_id": order},
    }
