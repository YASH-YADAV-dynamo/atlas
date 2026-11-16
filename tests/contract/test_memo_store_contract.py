"""Answer memos (SHIP_PLAN 6): `put_memo` / `get_memo` behave the same on the in-memory fake and HydraDB."""

from __future__ import annotations

from datetime import UTC, datetime

from atlas.contracts.types import Memo
from atlas.core.ids import memo_id
from tests.support.builders import SITE

AT = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)


def make(ns: str, key: str = "a:1:abc", url: str = f"https://{SITE}/wiki/Page", title: str = "Page") -> Memo:
    return Memo(id=memo_id(ns, SITE, key), site=SITE, key=key, url=url, title=title, at=AT)


def test_a_memo_round_trips(any_store, ns):
    memo = make(ns)
    assert any_store.get_memo(ns, memo.id) is None
    any_store.put_memo(ns, memo)
    assert any_store.get_memo(ns, memo.id) == memo


def test_putting_again_replaces_and_is_idempotent(any_store, ns):
    first = make(ns)
    any_store.put_memo(ns, first)
    any_store.put_memo(ns, first)
    assert any_store.get_memo(ns, first.id) == first
    newer = first.model_copy(update={"url": f"https://{SITE}/wiki/Other", "title": ""})
    any_store.put_memo(ns, newer)
    assert any_store.get_memo(ns, first.id) == newer


def test_memos_are_scoped_to_their_namespace(any_store, ns):
    memo = make(ns)
    any_store.put_memo(ns, memo)
    assert any_store.get_memo(f"{ns}x", memo.id) is None


def test_distinct_keys_are_distinct_memos(any_store, ns):
    one, two = make(ns, "a:1:abc"), make(ns, "t:1", f"https://{SITE}/wiki/{{topic}}", "")
    any_store.put_memo(ns, one)
    any_store.put_memo(ns, two)
    assert any_store.get_memo(ns, one.id) == one
    assert any_store.get_memo(ns, two.id) == two


def test_a_long_url_and_odd_characters_survive(any_store, ns):
    url = f"https://{SITE}/w/index.php?search=" + "a%20b'\"c" * 200
    memo = make(ns, url=url[:2048], title='It\'s "quoted"')
    any_store.put_memo(ns, memo)
    assert any_store.get_memo(ns, memo.id) == memo


def test_a_memo_is_not_a_state_or_goal(any_store, ns):
    any_store.put_memo(ns, make(ns))
    view = any_store.get_map(ns, SITE)
    assert (view.states, view.transitions) == ((), ())
    assert any_store.list_sites(ns) == () if hasattr(any_store, "list_sites") else True
