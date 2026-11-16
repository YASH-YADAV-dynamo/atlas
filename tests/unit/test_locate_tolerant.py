"""Tolerant locate (SHIP_PLAN 4.1): a known page with a rotating banner, a late-rendered control or
a cookie notice is still the known page; an exact signature hit always wins; nothing is stored
for a variant. Targets atlas.core.locate.locate / resolve."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from atlas.adapters.hydradb.fake import InMemoryGraphStore
from atlas.contracts.api_models import LOCATE_PATH
from atlas.contracts.types import Element, Snapshot
from atlas.core.locate import locate, resolve
from atlas.core.signature import fingerprint, state_from_snapshot, tokens
from tests.support.builders import SITE, click, learn
from tests.support.hosted import DEMO, bearer, hosted, hosted_settings

WORDS = [f"control {name}" for name in "abcdefghijklmnopqrstuvwx"]


def snap(names: list[str], path: str = "/home") -> Snapshot:
    return Snapshot(url=f"https://{SITE}{path}", elements=tuple(Element(role="link", name=n) for n in names))


BASE = WORDS[:20]
BANNER = [*BASE, "Donate to the fund today", "hide"]


def test_banner_added_or_removed_resolves_to_the_same_state(fake_store, ns):
    first = locate(fake_store, ns, SITE, snap(BASE))
    with_banner = locate(fake_store, ns, SITE, snap(BANNER))
    assert (with_banner.state_id, with_banner.known, with_banner.matched) == (first.state_id, True, "similar")

    other, store = "t2", InMemoryGraphStore()
    seeded = locate(store, other, SITE, snap(BANNER))
    without = locate(store, other, SITE, snap(BASE))
    assert (without.state_id, without.matched) == (seeded.state_id, "similar")


def test_below_the_threshold_is_a_new_state(fake_store, ns):
    first = locate(fake_store, ns, SITE, snap(BASE))
    changed = [*BASE[:16], "one more", "two more", "three more", "four more"]  # Jaccard 16/24
    second = locate(fake_store, ns, SITE, snap(changed))
    assert (second.known, second.matched) == (False, None)
    assert second.state_id != first.state_id


def test_another_url_template_never_matches(fake_store, ns):
    locate(fake_store, ns, SITE, snap(BASE, "/home"))
    other = locate(fake_store, ns, SITE, snap(BANNER, "/elsewhere"))
    assert (other.known, other.matched) == (False, None)


def test_pages_under_eight_tokens_are_exact_only(fake_store, ns):
    small = WORDS[:6]
    first = locate(fake_store, ns, SITE, snap(small))
    second = locate(fake_store, ns, SITE, snap([*small, "hide"]))  # Jaccard 6/7 = 0.857
    assert second.matched is None and second.state_id != first.state_id


def test_two_close_candidates_give_no_match(fake_store, ns):
    left = locate(fake_store, ns, SITE, snap([*BASE, "left one", "left two"]))
    right = locate(fake_store, ns, SITE, snap([*BASE, "right one", "right two"]))  # 20/24 to left
    assert right.state_id != left.state_id and right.matched is None
    probe = locate(fake_store, ns, SITE, snap([*BASE, "left one", "right one"]))  # 21/23 to both
    assert (probe.known, probe.matched) == (False, None)


def test_the_variant_is_not_stored(fake_store, ns):
    locate(fake_store, ns, SITE, snap(BASE))
    before = fake_store.get_map(ns, SITE).states
    locate(fake_store, ns, SITE, snap(BANNER))
    variant = state_from_snapshot(ns, SITE, snap(BANNER))
    assert fake_store.get_state(ns, variant.id) is None
    assert fake_store.get_map(ns, SITE).states == before


class WriteSpy:
    """A store proxy that fails the test on any write."""

    READS = frozenset({"get_state", "states_by_url", "get_map"})

    def __init__(self, inner: InMemoryGraphStore) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        assert name in self.READS, f"write during a read-only locate: {name}"
        return getattr(self.inner, name)


def test_resolve_writes_nothing(fake_store, ns):
    locate(fake_store, ns, SITE, snap(BASE))
    spy = WriteSpy(fake_store)
    assert resolve(spy, ns, SITE, snap(BANNER)).matched == "similar"  # type: ignore[arg-type]
    assert resolve(spy, ns, SITE, snap(WORDS)).known is False  # type: ignore[arg-type]


def test_a_read_only_key_writes_nothing_but_still_matches(tmp_path: Path, fake_store, ns):
    locate(fake_store, ns, SITE, snap(BASE))
    before = fake_store.get_map(ns, SITE)
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        for names in (BANNER, WORDS):
            body = {"site": SITE, "snapshot": snap(names).model_dump(mode="json")}
            response = client.post(LOCATE_PATH, json=body, headers=bearer(DEMO))
            assert response.status_code == 200, response.text
    assert fake_store.get_map(ns, SITE) == before


def test_fingerprint_is_backfilled_on_an_exact_hit(fake_store, ns):
    page = snap(BASE)
    state = state_from_snapshot(ns, SITE, page)
    fake_store.upsert_state(ns, state)  # written before fingerprints existed
    assert fake_store.states_by_url(ns, SITE, state.url_tmpl) == ((state.id, ""),)
    assert locate(fake_store, ns, SITE, page).known is True
    assert fake_store.states_by_url(ns, SITE, state.url_tmpl) == ((state.id, fingerprint(tokens(page))),)
    assert locate(fake_store, ns, SITE, snap(BANNER)).matched == "similar"


def test_url_only_snapshot_uses_the_state_with_most_successful_steps(fake_store, ns):
    busy, idle = snap(BASE), snap([*WORDS[4:20], "x one", "x two", "x three", "x four"])
    learn(fake_store, ns, busy, click("alpha", "link"), snap(["done"], "/done"))
    learn(fake_store, ns, idle, click("epsilon", "link"), snap(["done"], "/done"), ok=False)
    light = Snapshot(url=f"https://{SITE}/home", title="Home")
    found = locate(fake_store, ns, SITE, light)
    assert (found.state_id, found.matched) == (state_from_snapshot(ns, SITE, busy).id, "url")


def test_full_snapshot_uses_a_url_only_state_when_it_is_the_only_one(fake_store, ns):
    light = locate(fake_store, ns, SITE, Snapshot(url=f"https://{SITE}/home"))
    full = locate(fake_store, ns, SITE, snap(BASE))
    assert (full.state_id, full.matched) == (light.state_id, "url")


@settings(max_examples=40, deadline=None)
@given(
    pages=st.lists(
        st.sets(st.sampled_from(WORDS), min_size=8, max_size=24), min_size=1, max_size=6, unique_by=frozenset
    ),
    pick=st.integers(min_value=0),
)
def test_an_exact_hit_always_wins(pages, pick):
    store, ns = InMemoryGraphStore(), "prop"
    snaps = [snap(sorted(names)) for names in pages]
    ids = [locate(store, ns, SITE, s).state_id for s in snaps]
    stored = {sid for sid in ids if store.get_state(ns, sid) is not None}
    chosen = pick % len(snaps)
    exact = state_from_snapshot(ns, SITE, snaps[chosen]).id
    if exact in stored:
        again = locate(store, ns, SITE, snaps[chosen])
        assert (again.state_id, again.matched) == (exact, None)
