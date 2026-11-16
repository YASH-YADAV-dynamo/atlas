"""Smoke-test a hosted Atlas MCP over both routes (Authorization header and key in the URL path).

    python scripts/mcp_smoke.py --url https://atlas.yashdynamo.fun --key "$ATLAS_KEY"
    ATLAS_URL=... ATLAS_KEY=... python scripts/mcp_smoke.py

With no key it fetches the public demo key from /v1/mcp-info (read-only: only listing tools,
finding skills and planning are checked). With a private key it also reports a step and recalls
the entity it created. Keys are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import CallToolResult, TextContent

SITE = "testmart"
EXPECTED = {"atlas_find_skills", "atlas_recall", "atlas_plan", "atlas_report"}


def snapshot(site: str, path: str, *controls: str) -> dict[str, Any]:
    elements = [{"role": c.split(":", 1)[0], "name": c.split(":", 1)[1]} for c in controls]
    return {"url": f"https://{site}{path}", "title": path, "elements": elements}


@asynccontextmanager
async def connect(base: str, key: str, route: str) -> AsyncIterator[Client]:
    if route == "header":
        endpoint, headers = f"{base}/mcp", {"Authorization": f"Bearer {key}"}
    else:
        endpoint, headers = f"{base}/mcp/k/{key}", {}
    async with (
        create_mcp_http_client(headers=headers) as http,
        Client(streamable_http_client(endpoint, http_client=http)) as client,
    ):
        yield client


def payload(result: CallToolResult) -> dict[str, Any]:
    text = cast(TextContent, result.content[0]).text
    if result.is_error:
        raise AssertionError(f"tool error: {text[:200]}")
    return json.loads(text)


async def check_route(base: str, key: str, route: str, writable: bool, site: str) -> None:
    async with connect(base, key, route) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names >= EXPECTED, f"missing tools: {sorted(EXPECTED - names)}"
        skills_args = {"site": site, "goal_text": "Open order 1042"}
        found = payload(await client.call_tool("atlas_find_skills", skills_args))
        print(f"  [{route}] tools={len(names)} skills={len(found.get('skills', []))}")
        goal = f"Open order {uuid.uuid4().hex[:6]}"
        start = snapshot(site, "/orders", "link:Order 1042", "button:Search")
        planned = payload(
            await client.call_tool(
                "atlas_plan", {"site": site, "snapshot_summary": start, "goal_text": goal, "params": {}}
            )
        )
        print(f"  [{route}] plan needs_model={planned.get('needs_model')}")
        if not writable:
            return
        entity = uuid.uuid4().hex[:8]
        step = {
            "site": site,
            "from_state": planned["state_id"],
            "action": {"kind": "click", "locator": {"role": "link", "name": f"Order {entity}"}},
            "to_snapshot_summary": snapshot(site, f"/orders/{entity}", "button:Refund"),
            "ok": True,
            "goal_text": f"Open order {entity}",
            "params": {"order_id": entity},
            "goal_reached": True,
        }
        reported = payload(await client.call_tool("atlas_report", step))
        recalled = payload(await client.call_tool("atlas_recall", {"entity_type": "order_id", "key": entity}))
        assert recalled.get("found"), "reported entity was not recalled"
        print(f"  [{route}] report ok (new state {reported.get('state_id')}), recall found")


def resolve_key(base: str, key: str | None) -> tuple[str, bool]:
    if key:
        return key, True
    info = httpx.get(f"{base}/v1/mcp-info", timeout=20).json()
    demo = info.get("demo_key")
    if not demo:
        sys.exit("no key given and the server publishes no demo key")
    return str(demo), False


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default=os.environ.get("ATLAS_URL"), help="base URL, e.g. https://host")
    parser.add_argument("--key", default=os.environ.get("ATLAS_KEY"), help="private key (else the demo key)")
    parser.add_argument("--site", default=os.environ.get("ATLAS_SITE", SITE), help="the server's site name")
    args = parser.parse_args()
    if not args.url:
        parser.error("--url or ATLAS_URL is required")
    base = args.url.rstrip("/").removesuffix("/mcp")
    key, writable = resolve_key(base, args.key)
    print(f"{base}: {'private' if writable else 'demo (read-only)'} key")
    failed = 0
    for route in ("header", "path"):
        try:
            await check_route(base, key, route, writable, args.site)
        except Exception as error:  # report every route, then fail
            failed += 1
            print(f"  [{route}] FAILED: {type(error).__name__}: {str(error).replace(key, '***')[:300]}")
    print("OK" if not failed else f"{failed} route(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
