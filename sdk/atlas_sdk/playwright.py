"""A ready Playwright driver for the SDK: role/name snapshots in, role/name actions out.

    from playwright.sync_api import sync_playwright
    from atlas_sdk import Atlas
    from atlas_sdk.playwright import PlaywrightDriver

    with sync_playwright() as pw:
        page = pw.chromium.launch().new_page()
        page.goto("https://en.wikipedia.org/")
        atlas = Atlas("https://atlas.yashdynamo.fun", api_key, site="en.wikipedia.org")
        print(atlas.session(PlaywrightDriver(page)).run(
            "Open the article about {topic}", {"topic": "Hrithik Roshan"}, fallback=my_agent_step))

Needs `pip install playwright && playwright install chromium`.
"""

# ruff: noqa: ANN401  (wraps an untyped Playwright Page)
from __future__ import annotations

import contextlib
import json
import re
from typing import Any
from urllib.parse import urljoin

Json = dict[str, Any]

ROLES = (
    "link|button|textbox|searchbox|combobox|checkbox|radio|tab|menuitem|option|switch|"
    "spinbutton|slider|listbox|heading"
)
_ELEMENT = re.compile(rf'^(\s*)- ({ROLES}) "((?:[^"\\]|\\.)*)"', re.MULTILINE)
_LIST_PARENT = re.compile(r"^(\s*)- (listitem|row|article)\b", re.MULTILINE)
MAX_ELEMENTS = 400
MAX_TEXT = 4000


class PlaywrightDriver:
    """Wraps a sync Playwright `Page`. `settle_ms` waits for client-side rendering after an action."""

    def __init__(self, page: Any, *, settle_ms: int = 300, text: bool = True) -> None:
        self.page = page
        self.settle_ms = settle_ms
        self.text = text
        self._nav: tuple[str, int] | None = None  # (url, status) of the last main-frame navigation
        with contextlib.suppress(Exception):  # a page without events just sends no status
            page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        with contextlib.suppress(Exception):
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self._nav = (response.url.split("#")[0], int(response.status))

    def _status(self) -> int | None:
        """The main-frame response status of the current page, when known."""
        if self._nav and self._nav[0] == self.page.url.split("#")[0]:
            return self._nav[1]
        try:  # page loaded before the driver listened: the browser's own navigation timing
            value = self.page.evaluate("performance.getEntriesByType('navigation')[0]?.responseStatus")
            return int(value) if value else None
        except Exception:
            return None

    def snapshot(self) -> Json:
        tree = self.page.locator("body").aria_snapshot()
        elements: list[Json] = []
        list_depths: list[int] = []
        for line in tree.splitlines():
            parent = _LIST_PARENT.match(line)
            indent = len(line) - len(line.lstrip())
            while list_depths and indent <= list_depths[-1]:
                list_depths.pop()
            if parent:
                list_depths.append(indent)
                continue
            found = _ELEMENT.match(line)
            if not found or found.group(2) == "heading":
                continue
            name = json.loads(f'"{found.group(3)}"')[:200]
            item: Json = {"role": found.group(2), "name": name}
            if list_depths and found.group(2) == "link":
                item["in_list"] = True  # repeated data (results, rows): names are data, not structure
            elements.append(item)
            if len(elements) >= MAX_ELEMENTS:
                break
        snap: Json = {"url": self.page.url, "title": self.page.title()[:200], "elements": elements}
        status = self._status()
        if status:
            snap["status"] = status
        if self.text:
            snap["text"] = self.page.locator("body").inner_text()[:MAX_TEXT]
        return snap

    def act(self, action: Json) -> None:
        page = self.page
        for fill in action.get("fills", ()):
            self._set(self._find(fill["locator"]), fill.get("kind", "fill"), fill.get("value") or "")
        kind = action["kind"]
        if kind == "goto":
            page.goto(urljoin(page.url, action["value"]))
        elif kind == "press":
            target = self._find(action["locator"]) if action.get("locator") else page.keyboard
            target.press(action.get("value") or "Enter")
        elif kind in ("fill", "select", "check"):
            self._set(self._find(action["locator"]), kind, action.get("value") or "")
        else:
            self._find(action["locator"]).click()
        page.wait_for_load_state("load")
        if self.settle_ms:
            page.wait_for_timeout(self.settle_ms)

    def _set(self, target: Any, kind: str, value: str) -> None:
        if kind == "select":
            target.select_option(label=value)
        elif kind == "check":
            target.set_checked(value.lower() not in ("false", "0", "off", "no"))
        else:
            target.fill(value)

    def _find(self, locator: Json) -> Any:
        scope = self.page
        if locator.get("row"):
            scope = self.page.locator("tr, li, article").filter(has_text=locator["row"])
        return scope.get_by_role(locator["role"], name=locator["name"], exact=True).first
