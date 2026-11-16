"""State signature (ADR-0002). Targets atlas.core.signature and atlas.core.ids."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from atlas.contracts.types import MAX_ID, Element, Snapshot
from atlas.core.signature import signature, state_from_snapshot, url_template

# Control names without digits, spaces or pagination words, so no two names normalise alike.
LETTERS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=20).filter(
    lambda name: name not in {"next", "previous"}
)
ROLES = st.sampled_from(["button", "link", "textbox", "checkbox", "combobox"])
CONTROLS = st.lists(st.builds(Element, role=ROLES, name=LETTERS), min_size=1, max_size=8)
DATA_TEXT = st.text(min_size=0, max_size=30)
ROWS = st.lists(
    st.builds(Element, role=st.just("link"), name=DATA_TEXT, in_list=st.just(True)), min_size=1, max_size=15
)
RECORD_IDS = st.integers(min_value=1, max_value=10**9)


@given(CONTROLS, ROWS, ROWS, RECORD_IDS, RECORD_IDS, DATA_TEXT, DATA_TEXT)
def test_same_page_with_different_data_is_the_same_state(
    controls, rows_a, rows_b, id_a, id_b, text_a, text_b
):
    """R-SIG-1: record ids in the URL, list contents, title and page text do not change the state."""
    a = Snapshot(
        url=f"https://shop.test/orders/{id_a}", title=text_a, elements=(*controls, *rows_a), text=text_a
    )
    b = Snapshot(
        url=f"https://shop.test/orders/{id_b}", title=text_b, elements=(*controls, *rows_b), text=text_b
    )
    assert signature(a) == signature(b)
    assert state_from_snapshot("ns", "shop.test", a).id == state_from_snapshot("ns", "shop.test", b).id


@given(CONTROLS, ROWS, st.randoms(use_true_random=False))
def test_element_order_does_not_matter(controls, rows, rng):
    """R-SIG-2: the order in which the accessibility tree lists elements is irrelevant."""
    elements = [*controls, *rows]
    shuffled = list(elements)
    rng.shuffle(shuffled)
    url = "https://shop.test/orders"
    assert signature(Snapshot(url=url, elements=tuple(elements))) == signature(
        Snapshot(url=url, elements=tuple(shuffled))
    )


@given(CONTROLS, LETTERS, ROLES)
def test_added_or_removed_control_is_a_different_state(controls, extra_name, extra_role):
    """R-SIG-3: a page that gains or loses a control (outside lists) is a different state."""
    extra = Element(role=extra_role, name=extra_name)
    if extra in controls:
        controls = [c for c in controls if c != extra]
    url = "https://shop.test/orders/7"
    without = Snapshot(url=url, elements=tuple(controls))
    with_extra = Snapshot(url=url, elements=(*controls, extra))
    assert signature(without) != signature(with_extra)


def test_pagination_does_not_multiply_states():
    """R-SIG-4: page number and previous/next controls do not create new states (Release 0 rule)."""
    nav = (Element(role="link", name="Orders"), Element(role="button", name="Search"))
    row = (Element(role="link", name="View order", in_list=True),)
    first = Snapshot(
        url="https://shop.test/orders", elements=(*nav, *row, Element(role="link", name="Next page"))
    )
    middle = Snapshot(
        url="https://shop.test/orders?page=2",
        elements=(
            *nav,
            *row,
            Element(role="link", name="Previous page"),
            Element(role="link", name="Next page"),
        ),
    )
    last = Snapshot(
        url="https://shop.test/orders?page=3",
        elements=(*nav, *row, Element(role="link", name="Previous page")),
    )
    assert signature(first) == signature(middle) == signature(last)


def test_url_template_rules():
    """R-SIG-5: ids become {}, host is dropped, query keeps sorted non-empty keys but no values."""
    assert url_template("https://shop.test/orders/1042") == "/orders/{}"
    assert url_template("http://other.host:8100/orders/7/refund") == "/orders/{}/refund"
    assert url_template("https://shop.test/orders?q=1042") == "/orders?q"
    assert url_template("https://shop.test/orders?q=") == "/orders"
    assert url_template("https://shop.test/orders?q=a&page=2") == "/orders?q"
    assert url_template("https://shop.test/orders?z=1&a=2") == "/orders?a&z"
    assert url_template("https://shop.test/") == "/"
    assert url_template("https://shop.test/settings/general/") == "/settings/general"


def test_search_results_are_a_different_state_from_the_full_list():
    """R-SIG-5: a filtered list (?q=) is its own state, so 'search' is a real, plannable step."""
    elements = (Element(role="button", name="Search"), Element(role="link", name="View order", in_list=True))
    full = Snapshot(url="https://shop.test/orders", elements=elements)
    filtered = Snapshot(url="https://shop.test/orders?q=1042", elements=elements)
    assert signature(full) != signature(filtered)


@given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20), CONTROLS)
def test_state_ids_are_valid_and_scoped(ns_a, ns_b, controls):
    """R-SIG-6: state ids are positive 53-bit integers and differ across namespaces and sites."""
    snapshot = Snapshot(url="https://shop.test/x", elements=tuple(controls))
    state = state_from_snapshot(ns_a, "shop.test", snapshot)
    assert 1 <= state.id <= MAX_ID
    assert state.sig == signature(snapshot)
    assert state.url_tmpl == "/x"
    assert state.id != state_from_snapshot(ns_a, "other.test", snapshot).id
    if ns_a != ns_b:
        assert state.id != state_from_snapshot(ns_b, "shop.test", snapshot).id
