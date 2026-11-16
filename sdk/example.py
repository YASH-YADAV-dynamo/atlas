"""Run one TestMart task through Atlas. Needs the Atlas API, TestMart (python -m testmart) and
`pip install "atlas-route-sdk[playwright]"`. Environment: ATLAS_URL, ATLAS_API_KEY (optional), TESTMART_URL,
TESTMART_PASSWORD, ORDER_ID (an order number that is not refunded yet)."""

from __future__ import annotations

import json
import os

from atlas_sdk import Atlas
from atlas_sdk.drivers.playwright import PlaywrightDriver
from playwright.sync_api import sync_playwright

TESTMART = os.environ.get("TESTMART_URL", "http://127.0.0.1:8100")
SITE = "testmart.test"


def fallback(snapshot: dict, goal: str, params: dict) -> dict | None:
    """Your own agent goes here. This one prints the page and stops."""
    print(f"The map ends at {snapshot['url']} for '{goal}'. Page controls:")  # noqa: T201
    print(json.dumps(snapshot["elements"][:20]))  # noqa: T201
    return None


def main() -> None:
    url, key = os.environ.get("ATLAS_URL", "http://127.0.0.1:8000"), os.environ.get("ATLAS_API_KEY")
    atlas = Atlas(url, key, site=SITE)
    order = os.environ.get("ORDER_ID", "ORD-1001")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{TESTMART}/login")
        page.get_by_label("Username").fill("admin")
        page.get_by_label("Password").fill(os.environ.get("TESTMART_PASSWORD", "testmart"))
        page.get_by_role("button", name="Sign in").click()
        driver = PlaywrightDriver(page)
        summary = atlas.session(driver).run(f"Refund order {order}", {"order_id": order}, fallback=fallback)
        print(summary)  # noqa: T201
        browser.close()


if __name__ == "__main__":
    main()
