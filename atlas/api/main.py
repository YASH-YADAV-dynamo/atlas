"""ASGI entry point: `uvicorn atlas.api.main:app`. Wires the real store, a UTC clock, settings,
the demo router (runs the reference runner against this API), MCP at /mcp and the static UI."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from atlas.adapters.browser.local_playwright import LocalPlaywright
from atlas.adapters.hydradb.factory import open_configured_store
from atlas.adapters.model.factory import build_model_client
from atlas.adapters.model.ledger import ledger_from_settings
from atlas.api.app import create_app, mount_mcp
from atlas.contracts.types import RunResult, StepLog, TaskSpec
from atlas.demo.router import RunFn, build_demo_router
from atlas.mcp.keys import KeyRegistry
from atlas.mcp.logscrub import install_access_log_scrub
from atlas.runner.client import HttpRouteApi
from atlas.runner.run import run_task
from atlas.settings import Settings


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def make_run_fn(settings: Settings) -> RunFn:
    """Adapter for the demo router: one browser per run, the runner talking to this API over HTTP."""

    def run(task: TaskSpec, on_step: Callable[[StepLog], None], allowance: int) -> RunResult:
        api_key = settings.atlas_api_key.get_secret_value() if settings.atlas_api_key else None
        routes = HttpRouteApi(settings.atlas_api_url, api_key)
        model = build_model_client(settings)
        secrets = {
            "username": settings.testmart_user,
            "password": settings.testmart_password.get_secret_value(),
        }
        try:
            with LocalPlaywright(settings.testmart_url, headless=True) as driver:
                return run_task(
                    task,
                    routes,
                    driver,
                    model,
                    secrets=secrets,
                    max_steps=settings.run_max_steps,
                    max_model_calls=allowance,
                    on_step=on_step,
                )
        finally:
            routes.close()

    return run


install_access_log_scrub()  # keys in /mcp/k/<key> never reach the access log
_settings = Settings()
_store = open_configured_store(_settings)
_keys = KeyRegistry(_settings)  # one registry: REST and MCP resolve keys the same way
_clock = UtcClock()
app = create_app(_store, _clock, _settings, _keys)
app.include_router(
    build_demo_router(
        _settings, make_run_fn(_settings), budget_status=ledger_from_settings(_settings).status, store=_store
    )
)
mount_mcp(app, _store, _clock, _settings, _keys)
_web = Path(__file__).resolve().parents[2] / "web"
if _web.is_dir():  # mounted last so it never shadows the API routes
    app.mount("/", StaticFiles(directory=_web, html=True), name="web")
