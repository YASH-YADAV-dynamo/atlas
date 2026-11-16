"""State fingerprints (SHIP_PLAN 4.1): `set_state_tokens` / `states_by_url` behave the same on the
in-memory fake and HydraDB, and tolerant locate gives the same answers on both."""

from __future__ import annotations

from atlas.contracts.ports import STATES_BY_URL_LIMIT
from atlas.contracts.types import Element, Snapshot
from atlas.core.locate import locate
from atlas.core.signature import state_from_snapshot
from tests.support.builders import SITE, page

WORDS = tuple(f"control {name}" for name in "abcdefghijklmn")


def put(store, ns, path: str, *controls: str):
    state = state_from_snapshot(ns, SITE, page(path, *controls))
    store.upsert_state(ns, state)
    return state


def test_states_by_url_lists_the_template_newest_fingerprint_first(any_store, ns):
    a = put(any_store, ns, "/a", "button:one")
    b = put(any_store, ns, "/a", "button:two")
    put(any_store, ns, "/other", "button:one")
    assert sorted(any_store.states_by_url(ns, SITE, "/a")) == sorted([(a.id, ""), (b.id, "")])
    any_store.set_state_tokens(ns, a.id, "aaaa0000 bbbb1111")
    any_store.set_state_tokens(ns, b.id, "cccc2222")
    assert any_store.states_by_url(ns, SITE, "/a") == ((b.id, "cccc2222"), (a.id, "aaaa0000 bbbb1111"))
    any_store.set_state_tokens(ns, a.id, "aaaa0000 bbbb1111")  # rewriting makes it the newest
    assert [sid for sid, _ in any_store.states_by_url(ns, SITE, "/a")] == [a.id, b.id]


def test_states_by_url_is_scoped_and_bounded(any_store, ns):
    a = put(any_store, ns, "/a", "button:one")
    assert any_store.states_by_url(ns, "other.test", "/a") == ()
    assert any_store.states_by_url(f"{ns}x", SITE, "/a") == ()
    assert any_store.states_by_url(ns, SITE, "/missing") == ()
    assert len(any_store.states_by_url(ns, SITE, "/a")) <= STATES_BY_URL_LIMIT
    assert any_store.get_state(ns, a.id) == a  # fingerprints do not change the State contract
    any_store.set_state_tokens(ns, a.id, "")
    assert any_store.get_state(ns, a.id) == a


def test_tolerant_locate_is_the_same_on_both_stores(any_store, ns):
    base = Snapshot(url=f"https://{SITE}/home", elements=tuple(Element(role="link", name=w) for w in WORDS))
    banner = base.model_copy(update={"elements": (*base.elements, Element(role="link", name="Donate now"))})
    first = locate(any_store, ns, SITE, base)
    again = locate(any_store, ns, SITE, banner)
    assert (again.state_id, again.known, again.matched) == (first.state_id, True, "similar")
    assert len(any_store.states_by_url(ns, SITE, "/home")) == 1
