"""End-to-end smoke on the running stack: the reference runner completes a TestMart task
through the Route API, and a second task of the same kind needs fewer model calls.

Run with `make e2e` after starting HydraDB, TestMart and the API. Needs:
    ATLAS_E2E=1  ATLAS_API_URL  TESTMART_URL  TESTMART_DB (the server's SQLite file)
    ANTHROPIC_API_KEY  (and ATLAS_API_KEY when the API enforces one)
Skipped cleanly otherwise. Targets:
    atlas.runner.client.HttpRouteApi(base_url, api_key)
    atlas.adapters.browser.local_playwright.LocalPlaywright(base_url, headless=True)  (context manager)
    atlas.adapters.model.client.AnthropicModelClient(api_key, model_id)
    atlas.runner.run.run_task(task, routes, driver, model, *, secrets, max_steps) -> RunResult
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from atlas.contracts.types import StepSource, TaskSpec

pytestmark = pytest.mark.e2e
SEED = 42


@pytest.fixture
def stack():
    if os.environ.get("ATLAS_E2E") != "1":
        pytest.skip("ATLAS_E2E is not 1; end-to-end tests skipped")
    from atlas.settings import Settings

    settings = Settings()
    if settings.anthropic_api_key is None or "TESTMART_DB" not in os.environ:
        pytest.skip("ANTHROPIC_API_KEY and TESTMART_DB are required for the e2e smoke")
    httpx.post(f"{settings.testmart_url}/__reset", params={"seed": SEED}, timeout=30).raise_for_status()
    return settings


def refund_tasks(count: int):
    """`count` refund tasks for different orders, from the same seed the server was reset with."""
    from testmart.tasks import generate_tasks

    seen, picked = set(), []
    for task in generate_tasks(200, SEED):
        if task.template_id == "refund_order" and task.params["order_id"] not in seen:
            seen.add(task.params["order_id"])
            picked.append(task)
    assert len(picked) >= count
    return picked[:count]


def test_second_run_of_a_task_type_uses_fewer_model_calls(stack):
    """R-E2E-1: a TestMart task completes (checked in its DB); the next one rides the map."""
    from atlas.adapters.browser.local_playwright import LocalPlaywright
    from atlas.adapters.model.client import AnthropicModelClient
    from atlas.runner.client import HttpRouteApi
    from atlas.runner.run import run_task

    settings = stack
    site = f"testmart-{uuid.uuid4().hex[:8]}"  # a fresh map for this test
    api_key = settings.atlas_api_key.get_secret_value() if settings.atlas_api_key else None
    routes = HttpRouteApi(settings.atlas_api_url, api_key)
    model = AnthropicModelClient(settings.anthropic_api_key.get_secret_value(), settings.atlas_model_id)
    secrets = {"username": settings.testmart_user, "password": settings.testmart_password.get_secret_value()}

    results = []
    for task in refund_tasks(2):
        spec = TaskSpec(site=site, goal_text=task.text, params=task.params, start_path="/")
        with LocalPlaywright(settings.testmart_url, headless=True) as driver:
            result = run_task(spec, routes, driver, model, secrets=secrets, max_steps=settings.run_max_steps)
        assert result.success is True
        assert task.check(os.environ["TESTMART_DB"]) is True
        results.append(result)

    first, second = results
    assert first.model_calls >= 1
    assert second.model_calls < first.model_calls
    assert any(step.source is StepSource.MAP for step in second.steps)
    assert sum(step.source is StepSource.MODEL for step in second.steps) <= second.model_calls
