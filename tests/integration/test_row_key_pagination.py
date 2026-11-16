"""A row-keyed step holds for every page of a list (ADR-0002: the page number is not a state).

Regression for docs/rca/2026-11-16-param-reuse.md: the map step `click link 'View order' in row
'{order_id}'`, learned on ORD-1005 (page 1), must also work for ORD-1017 (page 2), and a key that
is in no row must fail at once instead of after the 10 s locator timeout.

Runs TestMart in-process on a free port with local Playwright Chromium; skipped cleanly when
the browser cannot be launched.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atlas.contracts.errors import BrowserActionError
from atlas.contracts.types import Action, ActionKind, Fill, Locator


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def testmart_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    uvicorn = pytest.importorskip("uvicorn")
    monkeypatch.setenv("TESTMART_DB", str(tmp_path / "testmart.db"))
    monkeypatch.setenv("TESTMART_PASSWORD", "testmart")  # local placeholder, not a secret
    monkeypatch.setenv("TESTMART_SEED", "42")
    from testmart.app import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def driver(testmart_url: str):
    from atlas.adapters.browser.local_playwright import LocalPlaywright

    try:
        session = LocalPlaywright(testmart_url).__enter__()
    except Exception as exc:  # no Chromium for Playwright on this machine
        pytest.skip(f"Playwright Chromium is not available: {type(exc).__name__}")
    try:
        session.act(Action(kind=ActionKind.GOTO, value="/login"))
        fills = (
            Fill(locator=Locator(role="textbox", name="Username"), value="admin"),
            Fill(locator=Locator(role="textbox", name="Password"), value="testmart"),
        )
        session.act(
            Action(kind=ActionKind.CLICK, locator=Locator(role="button", name="Sign in"), fills=fills)
        )
        session.act(Action(kind=ActionKind.GOTO, value="/orders"))
        yield session
    finally:
        session.__exit__(None, None, None)


def _view(order: str) -> Action:
    return Action(kind=ActionKind.CLICK, locator=Locator(role="link", name="View order", row=order))


def test_a_row_key_on_a_later_list_page_is_found(driver) -> None:
    driver.act(_view("ORD-1017"))  # 10 orders per page: ORD-1017 is on page 2
    assert driver.url().endswith("/orders/17")


def test_a_row_key_on_the_current_page_needs_no_paging(driver) -> None:
    driver.act(_view("ORD-1005"))
    assert driver.url().endswith("/orders/5")


def test_a_row_key_in_no_row_fails_fast(driver) -> None:
    started = time.monotonic()
    with pytest.raises(BrowserActionError):
        driver.act(_view("ORD-9999"))
    assert time.monotonic() - started < 9  # pages through the list, never one 10 s locator wait
