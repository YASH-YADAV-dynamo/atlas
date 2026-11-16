"""`python -m atlas.mcp [--http]`: serve the Atlas MCP tools.

Runner mode (default) opens one local Chromium on `settings.mcp_site`, signs in to TestMart with
the configured credentials before serving (the agent never sees them) and serves over stdio.
Planner mode has no browser. `--http` serves streamable HTTP in planner mode only, with one
namespace per key (admin, demo and self-serve keys, see docs/mcp.md); the Route API app
(`atlas.api.main`) mounts the same endpoint at /mcp.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor

from mcp.server.stdio import stdio_server

from atlas.adapters.hydradb.factory import open_configured_store
from atlas.contracts.mcp_models import Mode
from atlas.contracts.ports import BrowserDriver, Clock
from atlas.contracts.types import Action, ActionKind, Fill, Locator
from atlas.mcp.keys import KeyRegistry
from atlas.mcp.server import build_http_app, build_server, tenant_tools_factory
from atlas.mcp.tools import AtlasTools
from atlas.settings import Settings


class _UtcClock:
    def now(self):  # noqa: ANN202
        from datetime import UTC, datetime

        return datetime.now(UTC)


def _sign_in(driver: BrowserDriver, settings: Settings) -> None:
    driver.act(Action(kind=ActionKind.GOTO, value="/login"))
    driver.act(
        Action(
            kind=ActionKind.CLICK,
            locator=Locator(role="button", name="Sign in"),
            fills=(
                Fill(locator=Locator(role="textbox", name="Username"), value=settings.testmart_user),
                Fill(
                    locator=Locator(role="textbox", name="Password"),
                    value=settings.testmart_password.get_secret_value(),
                ),
            ),
        )
    )


async def _serve(args: argparse.Namespace) -> None:
    settings = Settings()
    mode = Mode.PLANNER if args.http else Mode(settings.mcp_mode)  # HTTP never drives a browser
    clock: Clock = _UtcClock()
    store = open_configured_store(settings)
    # A browser session stays on the thread that created it: one worker owns driver and tools.
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-mcp")
    loop = asyncio.get_running_loop()
    stack = None
    try:
        driver: BrowserDriver | None = None
        if mode is Mode.RUNNER:
            from contextlib import ExitStack

            playwright = importlib.import_module("atlas.adapters.browser.local_playwright")

            def open_browser() -> tuple[ExitStack, BrowserDriver]:
                exit_stack = ExitStack()
                browser = exit_stack.enter_context(playwright.LocalPlaywright(settings.testmart_url))
                _sign_in(browser, settings)
                return exit_stack, browser

            stack, driver = await loop.run_in_executor(worker, open_browser)
        tools = AtlasTools(
            store,
            clock,
            settings.atlas_namespace,
            settings.mcp_site,
            driver,
            max_steps=settings.run_max_steps,
        )
        if args.http:
            import uvicorn

            registry = KeyRegistry(settings)
            tenants = tenant_tools_factory(store, clock, settings.mcp_site, registry)
            app = build_http_app(tenants, mode, settings, keys=registry)
            config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
            await uvicorn.Server(config).serve()
        else:
            server = build_server(tools, mode, worker)
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
    finally:
        if stack is not None:
            await loop.run_in_executor(worker, stack.close)
        worker.shutdown(wait=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m atlas.mcp")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP at /mcp, planner mode")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
