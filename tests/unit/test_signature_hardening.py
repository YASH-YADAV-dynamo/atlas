"""Signature hardening (wave 4, ADR-0002 revision 2). Targets atlas.core.signature.

Property tests for the ways real pages vary without being a different page: numbers and
their formatting inside control names, numbered pagination, URL fragments and letter case
in ids.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from atlas.contracts.types import Element, Snapshot
from atlas.core.signature import signature, url_template

WORDS = st.sampled_from(["inbox", "cart", "total", "updated", "orders", "alerts", "balance", "due"])
ROLES = st.sampled_from(["button", "link", "textbox", "checkbox"])
INTS = st.integers(min_value=0, max_value=10**7)
NUMBERS = st.one_of(
    INTS.map(str),
    INTS.map(lambda n: f"{n:,}"),
    st.tuples(INTS, st.integers(0, 99)).map(lambda p: f"{p[0]:,}.{p[1]:02d}"),
    st.tuples(st.integers(0, 23), st.integers(0, 59)).map(lambda p: f"{p[0]}:{p[1]:02d}"),
    st.tuples(st.integers(1, 28), st.integers(1, 12), st.integers(2020, 2030)).map(
        lambda p: f"{p[0]}/{p[1]}/{p[2]}"
    ),
    st.tuples(st.integers(2020, 2030), st.integers(1, 12), st.integers(1, 28)).map(
        lambda p: f"{p[0]}-{p[1]:02d}-{p[2]:02d}"
    ),
)
BASE = (Element(role="link", name="Home"), Element(role="button", name="Save"))
ROWS = st.integers(1, 30).map(
    lambda n: tuple(Element(role="link", name="Open", in_list=True) for _ in range(n))
)


def snap(url: str, *elements: Element) -> Snapshot:
    return Snapshot(url=url, elements=(*BASE, *elements))


@given(WORDS, ROLES, NUMBERS, NUMBERS, st.sampled_from(["{w} ({n})", "{w}: {n}", "{n} {w}", "{w} ${n}"]))
def test_numbers_in_control_names_never_split_a_state(word, role, first, second, shape):
    """R-SIG-7: a counter, amount, time or date inside a control name, however it is formatted
    ("3", "1,299.00", "12:45", "2026-11-15"), is data: the state stays the same."""
    a = snap("https://shop.test/x", Element(role=role, name=shape.format(w=word, n=first)))
    b = snap("https://shop.test/x", Element(role=role, name=shape.format(w=word, n=second)))
    assert signature(a) == signature(b)


@given(WORDS, WORDS, ROLES, NUMBERS)
def test_different_words_are_still_different_controls(word_a, word_b, role, number):
    """R-SIG-7: number handling must not merge controls whose words differ."""
    assume(word_a != word_b)
    a = snap("https://shop.test/x", Element(role=role, name=f"{word_a} ({number})"))
    b = snap("https://shop.test/x", Element(role=role, name=f"{word_b} ({number})"))
    assert signature(a) != signature(b)


@given(
    st.integers(2, 40), st.integers(2, 40), ROWS, ROWS, st.sampled_from(["{n}", "Page {n}", "Go to page {n}"])
)
def test_numbered_pagination_does_not_split_a_list(pages_a, pages_b, rows_a, rows_b, label):
    """R-SIG-7: page-number links ("1 2 3 ...", "Page 4") are pagination, like previous/next:
    a one-page list and a forty-page list are the same state."""

    def listing(pages: int, rows) -> Snapshot:
        numbered = [Element(role="link", name=label.format(n=n)) for n in range(1, pages + 1)]
        return snap("https://shop.test/orders", *rows, *numbered, Element(role="link", name="Next"))

    single = snap("https://shop.test/orders", *rows_a)
    assert signature(listing(pages_a, rows_a)) == signature(listing(pages_b, rows_b)) == signature(single)


@given(ROWS, ROWS, st.integers(1, 500), st.integers(1, 500))
def test_long_lists_and_page_parameters_are_one_state(rows_a, rows_b, page_a, page_b):
    """R-SIG-4 (regression): row count and the page / offset / limit parameters never matter."""
    a = snap(f"https://shop.test/orders?page={page_a}&limit=10", *rows_a)
    b = snap(f"https://shop.test/orders?offset={page_b}", *rows_b)
    assert signature(a) == signature(b) == signature(snap("https://shop.test/orders", *rows_a))


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=12), st.integers(1, 10**6))
def test_url_fragment_and_trailing_noise_are_ignored(fragment, record):
    """R-SIG-7: a #fragment, a trailing slash or an empty query do not change the URL template."""
    base = f"https://shop.test/orders/{record}"
    assert url_template(f"{base}#{fragment}") == url_template(base) == "/orders/{}"
    assert url_template(f"{base}/?#") == "/orders/{}"
    assert "#" not in url_template(f"https://shop.test/orders?q=x#{fragment}")


@given(st.uuids(), st.uuids())
def test_opaque_ids_without_digits_are_still_ids(first, second):
    """R-SIG-7: a UUID or a long hex token is an id even in the rare case it has no digit."""
    token_a, token_b = "abcdefabcdefabcdefabcdefabcdefab", first.hex
    assert url_template(f"https://shop.test/files/{token_a}") == "/files/{}"
    assert url_template(f"https://shop.test/files/{token_b}") == "/files/{}"
    assert url_template(f"https://shop.test/files/{second}/view") == "/files/{}/view"


@given(st.sampled_from(["settings", "orders", "new", "edit", "general", "notifications", "more"]))
def test_ordinary_words_in_paths_are_never_ids(word):
    """R-SIG-7: id detection must not swallow real path words (short, no digits)."""
    assert url_template(f"https://shop.test/{word}/{word}") == f"/{word}/{word}"


@given(WORDS, ROLES)
def test_case_and_spacing_of_names_do_not_matter(word, role):
    """R-SIG-1 (regression): "Save  changes", "save changes" and " SAVE CHANGES " are one control."""
    variants = [f"{word} changes", f"{word.upper()}  CHANGES", f"  {word.title()}\tChanges "]
    sigs = {signature(snap("https://shop.test/x", Element(role=role, name=name))) for name in variants}
    assert len(sigs) == 1


def test_testmart_refunded_order_stays_a_separate_state():
    """R-SIG-3 (regression): hardening must keep 'order before refund' and 'after refund' apart."""
    common = (Element(role="link", name="View customer"), Element(role="link", name="Back to orders"))
    before = snap("https://shop.test/orders/12", *common, Element(role="link", name="Refund order"))
    after = snap("https://shop.test/orders/12", *common)
    assert signature(before) != signature(after)
