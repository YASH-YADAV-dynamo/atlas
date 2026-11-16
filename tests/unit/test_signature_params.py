"""Parameter-aware URL template and off-site host (SHIP_PLAN 4.2 fixes 1 and 3).
Targets atlas.core.signature."""

from __future__ import annotations

from atlas.contracts.types import Element, Snapshot
from atlas.core.signature import is_entity_page, signature, state_from_snapshot, url_template

WIKI = "en.wikipedia.org"


def article(title: str, *links: str) -> Snapshot:
    elements = tuple(Element(role="link", name=name) for name in (*links, "Edit", "Talk"))
    return Snapshot(url=f"https://{WIKI}/wiki/{title}", title=title, elements=elements)


def test_a_path_segment_equal_to_a_param_becomes_its_slot():
    for spelled in ("Hrithik_Roshan", "hrithik-roshan", "Hrithik%20Roshan", "HRITHIK+ROSHAN"):
        url = f"https://{WIKI}/wiki/{spelled}"
        assert url_template(url, {"topic": "Hrithik Roshan"}) == "/wiki/{topic}", spelled


def test_a_query_value_equal_to_a_param_becomes_its_slot():
    url = f"https://{WIKI}/w/index.php?search=Kajol&title=Special%3ASearch&page=2"
    assert url_template(url, {"topic": "kajol"}) == "/w/index.php?search={topic}&title"
    assert url_template(url) == "/w/index.php?search&title"


def test_entity_pages_of_different_subjects_are_one_state():
    hrithik = article("Hrithik_Roshan", "Hrithik Roshan filmography")
    a = state_from_snapshot("ns", WIKI, hrithik, {"topic": "Hrithik Roshan"})
    b = state_from_snapshot("ns", WIKI, article("Kajol", "Kajol filmography", "Dilwale"), {"topic": "Kajol"})
    assert a.id == b.id and a.url_tmpl == "/wiki/{topic}"


def test_pages_without_param_values_hash_as_before():
    page = article("Main_Page", "Contents", "Current events")
    assert signature(page, {"topic": "Kajol"}, WIKI) == signature(page)
    assert not is_entity_page(page, {"topic": "Kajol"}, WIKI)


def test_id_like_values_keep_token_identity():
    """An order page is `/orders/{}` either way, and a changed control on it is still a new state."""
    paid = Snapshot(url="https://shop.test/orders/77", elements=(Element(role="link", name="Refund order"),))
    renamed = paid.model_copy(update={"elements": (Element(role="link", name="Issue refund"),)})
    params = {"order_id": "77"}
    assert url_template(paid.url, params) == "/orders/{}"
    assert not is_entity_page(paid, params)
    assert signature(paid, params) != signature(renamed, params)


def test_an_off_site_host_is_part_of_the_template():
    assert url_template("https://www.bing.com/search?q=x", None, WIKI) == "//www.bing.com/search?q"
    assert url_template(f"https://{WIKI}/wiki/X", None, WIKI) == "/wiki/X"
    assert url_template("https://www.shop.test/a", None, "shop.test") == "/a"  # same site, subdomain
    assert url_template("https://127.0.0.1:8100/a", None, "testmart") == "/a"  # site is a label
    off = state_from_snapshot("ns", WIKI, Snapshot(url="https://other.org/wiki/X"))
    on = state_from_snapshot("ns", WIKI, Snapshot(url=f"https://{WIKI}/wiki/X"))
    assert off.id != on.id
