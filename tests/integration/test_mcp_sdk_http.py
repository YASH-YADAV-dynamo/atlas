"""The hosted MCP endpoint driven by the real MCP Python SDK client over streamable HTTP
(a real uvicorn server on a free port). Two tenants run concurrently, each in both protocol
eras (handshake with session ids, and the auto-negotiated one), and never see each other."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from atlas.api.app import create_app, mount_mcp
from atlas.mcp.keys import KeyRegistry
from tests.support.builders import FixedClock
from tests.support.hosted import DEMO, hosted_settings, plan_args, report_args


@contextmanager
def serving(store, settings) -> Iterator[str]:
    keys = KeyRegistry(settings)
    app = create_app(store, FixedClock(), settings, keys)
    mount_mcp(app, store, FixedClock(), settings, keys)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def result_json(result: Any) -> tuple[bool, Any]:
    text = result.content[0].text
    return bool(result.is_error), (text if result.is_error else json.loads(text))


@asynccontextmanager
async def connect(url: str, key: str, mode: Any = "auto") -> AsyncIterator[Client]:
    """The SDK client on the SDK's streamable-HTTP transport, with this key as bearer."""
    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {key}"}) as http,
        Client(streamable_http_client(f"{url}/mcp", http_client=http), mode=mode) as client,
    ):
        yield client


async def tenant_session(url: str, key: str, order: str, mode: str) -> dict[str, Any]:
    async with connect(url, key, mode) as client:
        names = sorted(tool.name for tool in (await client.list_tools()).tools)
        error, planned = result_json(await client.call_tool("atlas_plan", plan_args(order)))
        assert not error, planned
        await asyncio.sleep(0)  # let the other tenant interleave
        error, reported = result_json(
            await client.call_tool("atlas_report", report_args(planned["state_id"], order))
        )
        assert not error, reported
        seen: dict[str, bool] = {}
        for other in ("1042", "2001"):
            await asyncio.sleep(0)
            error, found = result_json(
                await client.call_tool("atlas_recall", {"entity_type": "order_id", "key": other})
            )
            assert not error, found
            seen[other] = found["found"]
        return {"tools": names, "seen": seen}


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_two_tenants_concurrently_through_the_sdk_client(any_store, ns, tmp_path, mode):
    with serving(any_store, hosted_settings(tmp_path, ns)) as url:
        keys = [
            httpx.post(f"{url}/v1/keys", headers={"X-Forwarded-For": ip}).json()["api_key"]
            for ip in ("10.9.0.1", "10.9.0.2")
        ]

        async def both() -> list[dict[str, Any]]:
            return await asyncio.gather(
                tenant_session(url, keys[0], "1042", mode), tenant_session(url, keys[1], "2001", mode)
            )

        first, second = asyncio.run(both())
    assert first["tools"] == ["atlas_find_skills", "atlas_plan", "atlas_recall", "atlas_report"]
    assert first["seen"] == {"1042": True, "2001": False}
    assert second["seen"] == {"1042": False, "2001": True}


def test_demo_key_through_the_sdk_client_cannot_report(fake_store, ns, tmp_path):
    with serving(fake_store, hosted_settings(tmp_path, ns)) as url:

        async def demo() -> tuple[bool, Any]:
            async with connect(url, DEMO) as client:
                return result_json(await client.call_tool("atlas_report", report_args(1)))

        error, message = asyncio.run(demo())
    assert error and "read-only" in message
