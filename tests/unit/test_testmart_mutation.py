"""TestMart mutation switch (SPEC 6): `POST /admin/mutate?v=N` (GET allowed) changes the UI
the way real sites change. Wave 3, lane R (Sonnet part). Skipped until the switch exists.

    v1  the original UI
    v2  the order page's "Refund order" link is renamed "Issue refund"; the "Customers" nav
        link moves to a new page behind a "More" nav link (the dashboard shortcut stays)
    v3  the original names, plus an interstitial: "Refund order" opens
        /orders/{id}/refund/review, whose "Continue to refund" link opens the confirm page
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

NAV = re.compile(r"<nav.*?</nav>", re.S)


@pytest.fixture
def shop(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTMART_DB", str(tmp_path / "testmart.db"))
    monkeypatch.setenv("TESTMART_PASSWORD", "unit-test-password")
    from testmart.app import app

    with TestClient(app) as client:
        if client.post("/admin/mutate", params={"v": 1}).status_code == 404:
            pytest.skip("wave 3: mutation switch not implemented yet")
        login = client.post("/login", data={"username": "admin", "password": "unit-test-password"})
        assert login.status_code == 200
        yield client


def links(html: str) -> dict[str, str]:
    """Link text -> href for every anchor on the page (last one wins)."""
    return {text.strip(): href for href, text in re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', html)}


def open_order(shop: TestClient) -> tuple[str, dict[str, str]]:
    """The first refundable order's detail URL and its links."""
    for href in re.findall(r'href="(/orders/\d+)"', shop.get("/orders").text):
        page = links(shop.get(href).text)
        if "Back to orders" in page and ({"Refund order", "Issue refund"} & page.keys()):
            return href, page
    raise AssertionError("no refundable order on the first page")


def test_switch_sets_and_reports_the_version(shop):
    """R-MUT-2: the switch accepts 1..3 by POST or GET, rejects anything else, and reset restores v1."""
    assert shop.post("/admin/mutate", params={"v": 2}).json() == {"ui_version": 2}
    assert 'data-ui-version="2"' in shop.get("/").text
    assert shop.get("/admin/mutate", params={"v": 3}).json() == {"ui_version": 3}
    assert 'data-ui-version="3"' in shop.get("/").text
    for bad in ("0", "4", "-1", "x"):
        assert shop.post("/admin/mutate", params={"v": bad}).status_code == 422
    assert shop.post("/admin/mutate").status_code == 422
    assert 'data-ui-version="3"' in shop.get("/").text
    shop.post("/__reset")
    assert 'data-ui-version="1"' in shop.get("/").text


def test_v2_renames_the_refund_link_and_moves_customers(shop):
    """R-MUT-2: v2 renames a control and moves a menu item; both old roads are gone, the pages
    are still reachable, and the task still works through the new names."""
    _, before = open_order(shop)
    assert "Refund order" in before and "Customers" in links(NAV.search(shop.get("/").text).group())

    shop.post("/admin/mutate", params={"v": 2})
    href, order = open_order(shop)
    assert "Issue refund" in order and "Refund order" not in order
    nav = links(NAV.search(shop.get("/").text).group())
    assert "Customers" not in nav and "More" in nav
    more = links(shop.get(nav["More"]).text)
    assert more["Customers"] == "/customers"
    assert "Go to customers" in links(shop.get("/").text)

    confirm = shop.get(order["Issue refund"])
    assert "Confirm refund" in confirm.text
    shop.post(order["Issue refund"])
    assert not {"Issue refund", "Refund order"} & links(shop.get(href).text).keys()


def test_v3_adds_an_interstitial_before_the_confirmation(shop):
    """R-MUT-2: v3 keeps the names but inserts a review page into the refund flow."""
    shop.post("/admin/mutate", params={"v": 3})
    href, order = open_order(shop)
    assert order["Refund order"] == f"{href}/refund/review"
    review = shop.get(order["Refund order"])
    assert review.status_code == 200 and "Confirm refund" not in review.text
    assert links(review.text)["Continue to refund"] == f"{href}/refund"
    assert "Confirm refund" in shop.get(f"{href}/refund").text
    shop.post(f"{href}/refund")
    assert "Refund order" not in links(shop.get(href).text)


def test_v1_is_restored_exactly(shop):
    """R-MUT-2: switching back to v1 gives the original pages (no leftovers from v2 or v3)."""
    original = {path: shop.get(path).text for path in ("/", "/orders", "/customers")}
    href, _ = open_order(shop)
    original[href] = shop.get(href).text
    for version in (2, 3, 1):
        shop.post("/admin/mutate", params={"v": version})
    assert {path: shop.get(path).text for path in original} == original
    assert shop.get("/more").status_code == 404
    assert "Continue to refund" not in shop.get(f"{href}/refund/review").text
