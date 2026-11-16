"""SHIP_PLAN 4.3 B, C3: blocked and error pages are never learned."""

from __future__ import annotations

import pytest

from atlas.contracts.types import Element, Snapshot
from atlas.core.blocked import blocked_reason
from tests.support.builders import GOAL, SITE, click, learn, page, state_id_of

HOME = page("/", "link:Orders", "button:Search")
ORDERS = page("/orders", "link:Order 1", "button:Filter")


def snap(url="https://shop.test/x", title="Hello", n=2, **extra) -> Snapshot:
    elements = tuple(Element(role="link", name=f"link {chr(97 + i)}") for i in range(n))
    return Snapshot(url=url, title=title, elements=elements, **extra)


@pytest.mark.parametrize(
    "snapshot",
    [
        snap(status=403),
        snap(status=503),
        snap("https://www.google.com/sorry/index"),
        snap("https://shop.test/captcha"),
        snap("https://shop.test/challenge?x=1"),
        snap("https://shop.test/cdn-cgi/l/chk"),
        snap(title="403 Forbidden"),
        snap(title="Access Denied"),
        snap(title="Just a moment..."),
        snap(title="Error 503 Service Unavailable"),
        snap(title="Are you a robot?", n=5),
    ],
)
def test_blocked_signals(snapshot):
    assert blocked_reason(snapshot) is not None


@pytest.mark.parametrize(
    "snapshot",
    [
        snap(status=200),
        snap(title="Sign in", n=3),
        snap("https://shop.test/login", title="Login"),
        snap(title="403 Forbidden", n=6),  # many controls: a real page
        snap(title="Orders"),
    ],
)
def test_real_pages_are_not_blocked(snapshot):
    assert blocked_reason(snapshot) is None


def test_blocked_page_stores_nothing_and_the_expected_edge_fails(fake_store, ns):
    expected = learn(fake_store, ns, HOME, click("Orders"), ORDERS).state_id
    blocked_page = snap("https://shop.test/sorry", title="Sorry", status=429)
    before = fake_store.get_map(ns, SITE)
    out = learn(
        fake_store,
        ns,
        HOME,
        click("Orders"),
        blocked_page,
        expect_state=expected,
        goal_text=GOAL,
        goal_reached=True,
    )
    assert out.blocked
    view = fake_store.get_map(ns, SITE)
    assert len(view.states) == len(before.states)
    assert len(view.transitions) == len(before.transitions)
    edge = next(t for t in view.transitions if t.to_state == expected)
    assert edge.ok == 1 and edge.fail == 1
    assert all(not fake_store.goal_targets(ns, g.id) for g in fake_store.list_goals(ns, SITE))
    # the blocked page is still not a state afterwards
    assert fake_store.get_state(ns, out.state_id) is None


def test_blocked_page_without_an_expected_edge_records_nothing(fake_store, ns):
    out = learn(fake_store, ns, HOME, click("Orders"), snap(status=403), goal_text=GOAL, goal_reached=True)
    assert out.blocked and out.transitions == ()
    assert len(fake_store.get_map(ns, SITE).transitions) == 0
    assert fake_store.list_goals(ns, SITE) == ()


def test_login_page_is_learned(fake_store, ns):
    login = page("/login", "textbox:Email", "textbox:Password", "button:Sign in")
    out = learn(fake_store, ns, HOME, click("Orders"), login)
    assert out.blocked is None
    assert state_id_of(fake_store, ns, login) == out.state_id
    assert fake_store.get_state(ns, out.state_id) is not None
