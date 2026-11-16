"""SHIP_PLAN 4.3 A1, A2, A3, A9: the snapshot normaliser truncates, it never rejects."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from atlas.contracts.types import Element, Snapshot
from atlas.core.signature import signature
from atlas.core.snapshot import MAX_ELEMENTS, normalise, normalise_raw, normalise_url


def raw(**over):
    return {"url": "https://shop.test/a", "title": "t", "elements": [], **over}


def test_keeps_the_first_400_elements_in_page_order():
    elements = [{"role": "link", "name": f"item {i}"} for i in range(450)]
    out = Snapshot.model_validate(normalise_raw(raw(elements=elements)))
    assert len(out.elements) == MAX_ELEMENTS
    assert [e.name for e in out.elements] == [f"item {i}" for i in range(400)]


def test_keeps_interactive_roles_only():
    elements = [
        {"role": "heading", "name": "Title"},
        {"role": "button", "name": "Buy"},
        {"role": "row", "name": "r"},
        {"role": "link", "name": "Home"},
    ]
    out = Snapshot.model_validate(normalise_raw(raw(elements=elements)))
    assert [e.role for e in out.elements] == ["button", "link"]


def test_dedupes_exact_duplicates_only():
    elements = [
        {"role": "link", "name": "More"},
        {"role": "link", "name": "More"},
        {"role": "link", "name": "More", "in_list": True},
        {"role": "button", "name": "More"},
    ]
    out = Snapshot.model_validate(normalise_raw(raw(elements=elements)))
    assert len(out.elements) == 3


def test_names_are_collapsed_and_cut_to_200():
    long_name = "word   " * 100
    out = Snapshot.model_validate(normalise_raw(raw(elements=[{"role": "link", "name": long_name}])))
    assert len(out.elements[0].name) == 200
    assert "  " not in out.elements[0].name


def test_text_and_title_are_cut():
    out = Snapshot.model_validate(normalise_raw(raw(text="x" * 9000, title="y" * 500)))
    assert len(out.text) == 4000 and len(out.title) == 200


def test_url_gets_a_scheme_and_a_lowercase_host():
    assert normalise_url("Example.COM/Path?Q=1") == "https://example.com/Path?Q=1"
    assert normalise_url("HTTP://Shop.Test/A") == "http://shop.test/A"
    assert normalise_url("//Shop.Test/a") == "https://shop.test/a"
    assert normalise_url("/orders/1") == "/orders/1"


def test_same_signature_as_the_trimmed_form():
    elements = [{"role": "link", "name": f"nav {i}"} for i in range(450)]
    out = Snapshot.model_validate(normalise_raw(raw(url="Shop.Test/a", title="", elements=elements)))
    trimmed = Snapshot(url="https://shop.test/a", elements=tuple(Element(**e) for e in elements[:400]))
    assert signature(out) == signature(trimmed) == signature(normalise(trimmed))


def test_a_url_over_2048_is_still_rejected():
    with pytest.raises(ValidationError):
        Snapshot.model_validate(normalise_raw(raw(url="https://shop.test/" + "a" * 2100)))


def test_bad_status_is_dropped_not_rejected():
    assert Snapshot.model_validate(normalise_raw(raw(status=99999))).status is None
    assert Snapshot.model_validate(normalise_raw(raw(status=404))).status == 404


ELEMENT = st.fixed_dictionaries(
    {
        "role": st.sampled_from(["link", "button", "heading", "textbox", "row", "x" * 60]),
        "name": st.text(max_size=600),
        "in_list": st.booleans(),
    }
)


@settings(max_examples=60, deadline=None)
@given(
    st.lists(ELEMENT, max_size=900),
    st.text(max_size=9000),
    st.text(max_size=600),
    st.text(alphabet="abcDEF.-", min_size=1, max_size=40),
)
def test_never_raises_on_size(elements, text, title, host):
    out = Snapshot.model_validate(
        normalise_raw({"url": host + "/p", "title": title, "text": text, "elements": elements})
    )
    assert len(out.elements) <= MAX_ELEMENTS
    assert all(len(e.name) <= 200 for e in out.elements)
    assert len(out.text) <= 4000 and len(out.title) <= 200
