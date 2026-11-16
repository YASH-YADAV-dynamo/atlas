"""Auth on the MCP streamable-HTTP transport. Targets (lane M):
    atlas.mcp.server.build_http_app(tools: AtlasTools, mode: Mode, settings: Settings) -> ASGI app
serving MCP at /mcp behind the same bearer key as the Route API."""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.mcp.server", reason="MCP wave: implemented by lane M")

from starlette.testclient import TestClient

from atlas.contracts.mcp_models import Mode
from atlas.mcp.server import build_http_app
from atlas.mcp.tools import AtlasTools
from atlas.settings import Settings
from tests.support.builders import SITE, FixedClock

KEY = "test-key-not-a-secret"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def app_with_key(store, ns, key: str | None):
    settings = Settings(_env_file=None, atlas_namespace=ns, atlas_api_key=key)  # type: ignore[call-arg]
    return build_http_app(AtlasTools(store, FixedClock(), ns, SITE, None), Mode.PLANNER, settings)


def test_mcp_http_requires_the_bearer_key(fake_store, ns):
    """R-MCP-8: without the configured key the MCP endpoint answers 401 and runs nothing."""
    with TestClient(app_with_key(fake_store, ns, KEY)) as client:
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 401
        wrong = {**HEADERS, "Authorization": "Bearer wrong"}
        assert client.post("/mcp", json=INITIALIZE, headers=wrong).status_code == 401
        right = {**HEADERS, "Authorization": f"Bearer {KEY}"}
        # With the key the request reaches the MCP transport; its own status is the SDK's business.
        assert client.post("/mcp", json=INITIALIZE, headers=right).status_code != 401


def test_mcp_http_refuses_to_start_without_a_key(fake_store, ns):
    """R-MCP-8: the hosted transport is never served unauthenticated."""
    from atlas.contracts.errors import UnauthorizedError

    with pytest.raises(UnauthorizedError):
        app_with_key(fake_store, ns, None)
