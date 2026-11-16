"""A tiny scripted back-office site that implements the BrowserDriver port for tests.

    home --Orders--> orders --[fill "Search orders", Search]--> results --View order--> detail
    detail --Refund order--> confirm --Confirm refund--> detail (refunded: no "Refund order")
    home --Settings--> settings --Orders--> orders        (every page: "Dashboard" -> home)

`renamed` and `dead` simulate a site change. `goto` supports "/" and "/settings" and does not count in `acts`.
It is a test double for a port, never for the unit under test. `refunded` and `typed`
record what really happened, so tests can check that bound parameter values were used.
"""

from __future__ import annotations

from atlas.contracts.errors import BrowserActionError
from atlas.contracts.types import Action, ActionKind, Element, Snapshot

BASE = "https://shop.test"
ALL_ORDERS = ("1001", "1002", "1003")


def _el(role: str, name: str, in_list: bool = False) -> Element:
    return Element(role=role, name=name, in_list=in_list)


class FakeSite:
    def __init__(
        self,
        start: str = "home",
        extra_controls: int = 0,
        renamed: dict[str, str] | None = None,
        dead: frozenset[str] = frozenset(),
    ) -> None:
        self._renamed = renamed or {}  # a UI change: control `old name` is now shown as `new name`
        self._dead = dead  # controls that are shown but fail when clicked
        self.page = start
        self.query = ""
        self.order = ""
        self.search_box = ""
        self.refunded: set[str] = set()
        self.typed: list[str] = []
        self.acts = 0
        self._extra = tuple(
            _el("link", f"Extra navigation entry number {i} with a long label") for i in range(extra_controls)
        )

    def url(self) -> str:
        paths = {
            "home": "/",
            "settings": "/settings",
            "orders": f"/orders?q={self.query}" if self.query else "/orders",
            "detail": f"/orders/{self.order}",
            "confirm": f"/orders/{self.order}/refund",
        }
        return BASE + paths[self.page]

    def snapshot(self) -> Snapshot:
        rows = (self.query,) if self.query else ALL_ORDERS
        elements = {
            "home": [_el("link", "Orders"), _el("link", "Settings")],
            "settings": [_el("link", "Orders"), _el("link", "Dashboard"), _el("checkbox", "Compact tables")],
            "orders": [
                _el("link", "Dashboard"),
                _el("textbox", "Search orders"),
                _el("button", "Search"),
                *[_el("link", "View order", in_list=True) for _ in rows],
            ],
            "detail": [_el("link", "Dashboard")]
            + ([] if self.order in self.refunded else [_el("link", "Refund order")]),
            "confirm": [_el("button", "Confirm refund"), _el("link", "Cancel")],
        }[self.page]
        status = "Refunded" if self.order in self.refunded else "Paid"
        text = f"Order {self.order} status {status}" if self.page in ("detail", "confirm") else self.page
        shown = [e.model_copy(update={"name": self._renamed.get(e.name, e.name)}) for e in elements]
        return Snapshot(url=self.url(), title=self.page.title(), elements=(*shown, *self._extra), text=text)

    def act(self, action: Action) -> None:
        texts = [action.value or "", *(f.value or "" for f in action.fills)]
        names = [
            loc.name + (loc.row or "") for loc in (action.locator, *(f.locator for f in action.fills)) if loc
        ]
        if action.param or any(f.param for f in action.fills) or any("{" in t for t in texts + names):
            raise BrowserActionError("unbound slot reached the browser")
        if action.kind is ActionKind.GOTO and action.value in ("/", "/settings"):
            self.page, self.query = ("home" if action.value == "/" else "settings"), ""
            return
        if action.kind is not ActionKind.CLICK or action.locator is None:
            raise BrowserActionError(f"unsupported action {action.kind}")
        for fill in action.fills:
            if (self.page, fill.locator.name) != ("orders", "Search orders") or fill.value is None:
                raise BrowserActionError(f"no field {fill.locator.name!r} here")
            self.search_box = fill.value
            self.typed.append(fill.value)
        name = action.locator.name
        if name in self._renamed or name in self._dead:
            raise BrowserActionError(f"control {name!r} did not respond")
        original = {new: old for old, new in self._renamed.items()}.get(name, name)
        self._click(original, action.locator.row)
        self.acts += 1

    def _click(self, name: str, row: str | None) -> None:
        key = (self.page, name)
        if name == "Dashboard" and self.page != "home" and self.page != "confirm":
            self.page, self.query = "home", ""
        elif key in (("home", "Orders"), ("settings", "Orders")):
            self.page, self.query, self.search_box = "orders", "", ""
        elif key == ("home", "Settings"):
            self.page = "settings"
        elif key == ("orders", "Search"):
            self.query = self.search_box
        elif key == ("orders", "View order"):
            self.order = self.query or row or ALL_ORDERS[0]
            self.page = "detail"
        elif key == ("detail", "Refund order") and self.order not in self.refunded:
            self.page = "confirm"
        elif key == ("confirm", "Confirm refund"):
            self.refunded.add(self.order)
            self.page = "detail"
        elif key == ("confirm", "Cancel"):
            self.page = "detail"
        else:
            raise BrowserActionError(f"no control {name!r} on {self.page}")
