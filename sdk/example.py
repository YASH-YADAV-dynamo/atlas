"""Run one TestMart task through Atlas. Needs the Atlas API, TestMart (python -m testmart) and
`pip install atlas-sdk playwright`. Environment: ATLAS_URL, ATLAS_API_KEY (optional), TESTMART_URL,
TESTMART_PASSWORD, ORDER_ID (an order number that is not refunded yet)."""

from __future__ import annotations

import json
import os
import re

from atlas_sdk import Atlas
from playwright.sync_api import Locator, Page, sync_playwright

TESTMART = os.environ.get("TESTMART_URL", "http://127.0.0.1:8100")
SITE = "testmart.test"
ELEMENT = re.compile(r'- (link|button|textbox|searchbox|checkbox|combobox) "((?:[^"\\]|\\.)*)"')


class PlaywrightDriver:
    """A Playwright page that speaks the SDK's plain dicts: role/name snapshots, acts by role and name."""

    def __init__(self, page: Page) -> None:
        self.page = page

    def snapshot(self) -> dict:
        tree = self.page.locator("body").aria_snapshot()
        elements = [{"role": role, "name": json.loads(f'"{name}"')} for role, name in ELEMENT.findall(tree)]
        return {"url": self.page.url, "title": self.page.title(), "elements": elements}

    def act(self, action: dict) -> None:
        for fill in action.get("fills", ()):
            self._find(fill["locator"]).fill(fill["value"])
        if action["kind"] == "goto":
            self.page.goto(TESTMART + action["value"])
            return
        target = self._find(action["locator"])
        if action["kind"] == "fill":
            target.fill(action["value"])
        else:
            target.click()
        self.page.wait_for_load_state("load")

    def _find(self, locator: dict) -> Locator:
        scope: Page | Locator = self.page
        if locator.get("row"):
            scope = self.page.locator("tr, li").filter(has_text=locator["row"])
        return scope.get_by_role(locator["role"], name=locator["name"], exact=True)


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
