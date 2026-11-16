"""BrowserDriver on local Playwright Chromium. Reads the accessibility tree and acts by role + name."""

from __future__ import annotations

import json
import re
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PwLocator

from atlas.contracts.errors import BrowserActionError
from atlas.contracts.types import Action, ActionKind, Element, Locator, Snapshot

INTERACTIVE = frozenset({"link", "button", "textbox", "searchbox", "checkbox", "combobox"})
TEXT_CAP = 4000
NAME_CAP = 200
ELEMENT_CAP = 400
ACTION_TIMEOUT_MS = 3_000  # a control from the snapshot is there at once; a missing one should fail fast
NAVIGATION_TIMEOUT_MS = 10_000
MAX_LIST_PAGES = 20  # pages of one list searched for a row key (pagination is one state, ADR-0002)
_NEXT_PAGE = re.compile(r"^\s*next(?: page)?\s*$", re.IGNORECASE)
_LINE = re.compile(r'^(\s*)- ([a-z]+)(?: ("(?:[^"\\]|\\.)*"))?')
_FALSY = frozenset({"false", "0", "off", "no", "unchecked"})


def parse_aria(yaml_text: str) -> tuple[Element, ...]:
    """Interactive elements from a Playwright ARIA snapshot (indented `- role "name"` lines)."""
    elements: list[Element] = []
    stack: list[tuple[int, str]] = []  # (indent, role) of open ancestors
    for line in yaml_text.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        indent, role, raw_name = len(match.group(1)), match.group(2), match.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if role in INTERACTIVE and len(elements) < ELEMENT_CAP:
            name = json.loads(raw_name) if raw_name else ""
            roles = [r for _, r in stack]
            in_list = "row" in roles or ("listitem" in roles and "navigation" not in roles)
            elements.append(Element(role=role, name=name[:NAME_CAP], in_list=in_list))
        stack.append((indent, role))
    return tuple(elements)


class LocalPlaywright:
    def __init__(self, base_url: str, headless: bool = True) -> None:
        self._base = base_url.rstrip("/")
        self._headless = headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> Self:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._page.set_default_timeout(ACTION_TIMEOUT_MS)
        self._page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                closer.close()
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self._context = self._page = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise BrowserActionError("browser session is not open")
        return self._page

    def url(self) -> str:
        return self.page.url

    def snapshot(self) -> Snapshot:
        page = self.page
        try:
            tree = page.locator("body").aria_snapshot()
            text = page.locator("body").inner_text()
            title = page.title()
        except PlaywrightError as exc:
            raise BrowserActionError("could not read the page") from exc
        return Snapshot(
            url=page.url or "about:blank",
            title=title[:NAME_CAP],
            elements=parse_aria(tree),
            text=text[:TEXT_CAP],
        )

    def act(self, action: Action) -> None:
        try:
            for fill in action.fills:
                self._apply(fill.kind, self._find(fill.locator), fill.value)
            if action.kind is ActionKind.GOTO:
                self.page.goto(self._base + (action.value or "/"))
            else:
                if action.locator is None:
                    raise BrowserActionError("action has no locator")
                self._apply(action.kind, self._find(action.locator), action.value)
            self.page.wait_for_load_state("load")
        except PlaywrightError as exc:
            raise BrowserActionError(f"{action.kind.value} failed: {type(exc).__name__}") from exc

    def _find(self, locator: Locator) -> PwLocator:
        if not locator.row:
            return self.page.get_by_role(locator.role, name=locator.name, exact=True)  # type: ignore[arg-type]
        # List pages share one state (ADR-0002), so a row-keyed step holds for every page of the
        # list: look for the row here, then on the following pages, and fail at once if absent.
        for _ in range(MAX_LIST_PAGES):
            scope = self._row(locator.row)
            if scope is not None:
                return scope.get_by_role(locator.role, name=locator.name, exact=True)  # type: ignore[arg-type]
            if not self._next_page():
                break
        raise BrowserActionError("no list row matches the row key")

    def _row(self, key: str) -> PwLocator | None:
        rows = self.page.locator("tr, li, [role=row], [role=listitem]")
        exact = rows.filter(has=self.page.get_by_text(key, exact=True))  # cell equals the key
        if exact.count():
            return exact
        loose = rows.filter(has_text=key)
        return loose if loose.count() else None

    def _next_page(self) -> bool:
        """Follow the list's next-page control; False if there is none or it leaves the list."""
        page = self.page
        for role in ("link", "button"):
            control = page.get_by_role(role, name=_NEXT_PAGE)  # type: ignore[arg-type]
            if control.count():
                path = urlsplit(page.url).path
                control.first.click()
                page.wait_for_load_state("load")
                if urlsplit(page.url).path == path:
                    return True
                page.go_back()  # a wizard's "Next", not pagination: stay on the list
                return False
        return False

    @staticmethod
    def _apply(kind: ActionKind, target: PwLocator, value: str | None) -> None:
        if kind is ActionKind.CLICK:
            target.click()
        elif kind is ActionKind.FILL:
            target.fill(_need(value))
        elif kind is ActionKind.SELECT:
            target.select_option(label=_need(value))
        elif kind is ActionKind.CHECK:
            target.set_checked(value is None or value.strip().lower() not in _FALSY)
        elif kind is ActionKind.PRESS:
            target.press(_need(value))
        else:
            raise BrowserActionError(f"unsupported action kind {kind.value}")


def _need(value: str | None) -> str:
    if value is None:
        raise BrowserActionError("action value is unbound")
    return value
