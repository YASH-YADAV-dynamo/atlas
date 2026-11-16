"""Session core (SHIP_PLAN 3.4, 6): `start` and `finish` on the in-memory store."""

from __future__ import annotations

import pytest

from atlas.contracts.api_models import FinishRequest, FinishStep, StartRequest
from atlas.contracts.errors import UnknownPlanError
from atlas.contracts.types import Action, ActionKind, Element, Locator, PlanReason, Snapshot
from atlas.core import memo, session
from atlas.core.ids import goal_id, memo_id
from atlas.core.params import goal_key
from tests.support.builders import FixedClock

SITE = "wiki.test"
GOAL = "Open the article about {topic}"
REWORDED = "Show the article about {topic}"
SHAH = {"topic": "Shah Rukh Khan"}
HRITHIK = {"topic": "Hrithik Roshan"}
CLOCK = FixedClock()


def snap(path: str, *controls: str, title: str = "") -> Snapshot:
    elements = tuple(Element(role=c.split(":", 1)[0], name=c.split(":", 1)[1]) for c in controls)
    return Snapshot(url=f"https://{SITE}{path}", title=title or path, elements=elements)


HOME = snap("/", "searchbox:Search", "button:Go", "link:Main page")


def results(topic: str) -> Snapshot:
    return snap(
        f"/w/index.php?search={topic.replace(' ', '+')}", f"link:{topic}", "button:Go", title="Results"
    )


def article(topic: str) -> Snapshot:
    return snap(f"/wiki/{topic.replace(' ', '_')}", "link:Edit", "link:Talk", title=f"{topic} - Wiki")


def fill(topic: str) -> Action:
    return Action(kind=ActionKind.FILL, locator=Locator(role="searchbox", name="Search"), value=topic)


def click(name: str, role: str = "button") -> Action:
    return Action(kind=ActionKind.CLICK, locator=Locator(role=role, name=name))


def begin(store, ns, params, *, goal=GOAL, snapshot=HOME, read_only=False):
    request = StartRequest(site=SITE, snapshot=snapshot, goal_text=goal, params=params)
    return session.start(store, ns, request, read_only=read_only)


def explore(store, ns, params=SHAH, *, kind="read", goal=GOAL, reached=True, ok=True):
    """A first visit: type the subject, press Go, open the article; reported with one finish."""
    topic = params["topic"]
    start = begin(store, ns, params, goal=goal)
    steps = (
        FinishStep(action=fill(topic), to=HOME),
        FinishStep(action=click("Go"), to=results(topic)),
        FinishStep(action=click(topic, "link"), to=article(topic)),
    )
    request = FinishRequest(
        site=SITE,
        from_state=start.state_id,
        steps=steps,
        ok=ok,
        goal_text=goal,
        params=params,
        goal_reached=reached,
        kind=kind,
    )
    return start, session.finish(store, CLOCK, ns, request)


def test_an_empty_map_needs_the_model(fake_store, ns):
    out = begin(fake_store, ns, SHAH)
    assert (out.needs_model, out.reason, out.route) == (True, PlanReason.UNKNOWN_GOAL, ())
    assert (out.plan_id, out.open_url, out.search_url, out.known) == (None, None, None, False)
    assert fake_store.get_state(ns, out.state_id) is not None


def test_a_read_only_key_stores_nothing(fake_store, ns):
    out = begin(fake_store, ns, SHAH, read_only=True)
    assert fake_store.get_state(ns, out.state_id) is None


def test_a_blocked_page_is_not_stored_or_planned(fake_store, ns):
    page = Snapshot(url=f"https://{SITE}/sorry/index", title="Captcha")
    out = begin(fake_store, ns, SHAH, snapshot=page)
    assert (out.blocked, out.needs_model, out.plan_id) == ("url_path:sorry", True, None)
    assert fake_store.get_state(ns, out.state_id) is None


def test_finish_learns_the_steps_and_folds_the_fill(fake_store, ns):
    _, done = explore(fake_store, ns)
    assert (done.learned, done.blocked, done.goal) == (2, None, goal_key(GOAL, SHAH))
    again = begin(fake_store, ns, HRITHIK)
    assert again.reason is PlanReason.ROUTE_FOUND
    assert len(again.route) == 2
    first = again.route[0].action
    assert first.kind is ActionKind.CLICK
    assert first.fills
    assert first.fills[0].param == "topic"


def test_same_subject_gets_the_remembered_answer_page(fake_store, ns):
    explore(fake_store, ns)
    out = begin(fake_store, ns, SHAH)
    assert out.open_url == f"https://{SITE}/wiki/Shah_Rukh_Khan"
    assert (out.match, out.needs_model, out.reason) == ("exact", False, PlanReason.ROUTE_FOUND)
    assert out.plan_id is not None
    assert len(out.plan_id) == 12


def test_new_subject_gets_the_bound_url_template(fake_store, ns):
    explore(fake_store, ns)
    out = begin(fake_store, ns, HRITHIK)
    assert out.open_url == f"https://{SITE}/wiki/Hrithik_Roshan"
    assert out.route  # the route is served as well; the caller may navigate instead
    odd = begin(fake_store, ns, {"topic": "AT&T / Bell"})
    assert odd.open_url == f"https://{SITE}/wiki/AT%26T_%2F_Bell"  # quoted into one path segment


def test_the_search_entry_is_learned_and_offered_on_an_unknown_goal(fake_store, ns):
    explore(fake_store, ns)
    other = begin(fake_store, ns, {"q": "birthplace"}, goal="Find the birthplace of {q}")
    assert other.reason is PlanReason.UNKNOWN_GOAL
    assert other.search_url == f"https://{SITE}/w/index.php?search={{q}}"
    assert begin(fake_store, ns, SHAH).search_url is None  # served from the map: no search offered


def test_an_act_goal_never_gets_an_address(fake_store, ns):
    explore(fake_store, ns, kind="act")
    out = begin(fake_store, ns, HRITHIK)
    assert out.open_url is None
    assert out.route
    assert fake_store.get_memo(ns, memo_id(ns, SITE, memo.SEARCH_KEY)) is None


def test_an_unreached_or_failed_read_stores_no_memo(fake_store, ns):
    explore(fake_store, ns, reached=False)
    assert begin(fake_store, ns, SHAH).open_url is None
    explore(fake_store, ns, params=HRITHIK, ok=False)
    assert begin(fake_store, ns, HRITHIK).open_url is None


def test_only_addresses_are_stored(fake_store, ns):
    explore(fake_store, ns)
    key = goal_key(GOAL, SHAH)
    stored = fake_store.get_memo(ns, memo_id(ns, SITE, memo.answer_key(goal_id(ns, SITE, key), SHAH)))
    assert stored is not None
    assert stored.url == f"https://{SITE}/wiki/Shah_Rukh_Khan"
    assert set(type(stored).model_fields) == {"id", "site", "key", "url", "title", "at"}


def test_a_page_that_does_not_follow_the_pattern_gets_no_template(fake_store, ns):
    start = begin(fake_store, ns, SHAH)
    odd = snap("/person/4711", "link:Edit", title="Shah Rukh Khan")  # the subject is not in the address
    request = FinishRequest(
        site=SITE,
        from_state=start.state_id,
        steps=(FinishStep(action=click("Go"), to=odd),),
        ok=True,
        goal_text=GOAL,
        params=SHAH,
        goal_reached=True,
        kind="read",
    )
    session.finish(fake_store, CLOCK, ns, request)
    assert begin(fake_store, ns, SHAH).open_url == f"https://{SITE}/person/4711"
    assert begin(fake_store, ns, HRITHIK).open_url is None


def test_replay_credit_counts_the_route_steps(fake_store, ns):
    explore(fake_store, ns)
    planned = begin(fake_store, ns, HRITHIK)
    request = FinishRequest(
        site=SITE,
        plan_id=planned.plan_id,
        done=2,
        ok=True,
        ms=200,
        goal_text=GOAL,
        params=HRITHIK,
        goal_reached=True,
        kind="read",
        to_snapshot=article("Hrithik Roshan"),
    )
    out = session.finish(fake_store, CLOCK, ns, request)
    assert out.learned == 2
    assert sorted(t.ok for t in fake_store.get_map(ns, SITE).transitions) == [2, 2]
    assert out.state_id == planned.route[-1].expect_state


def test_replay_credit_is_idempotent(fake_store, ns):
    explore(fake_store, ns)
    planned = begin(fake_store, ns, HRITHIK)
    request = FinishRequest(site=SITE, plan_id=planned.plan_id, done=2, ok=True)
    assert session.finish(fake_store, CLOCK, ns, request).learned == 2
    assert session.finish(fake_store, CLOCK, ns, request).learned == 0
    assert sorted(t.ok for t in fake_store.get_map(ns, SITE).transitions) == [2, 2]


def test_a_failed_replay_charges_the_next_step_once(fake_store, ns):
    explore(fake_store, ns)
    planned = begin(fake_store, ns, HRITHIK)
    request = FinishRequest(site=SITE, plan_id=planned.plan_id, done=1, ok=False)
    assert session.finish(fake_store, CLOCK, ns, request).learned == 1
    totals = sorted((t.ok, t.fail) for t in fake_store.get_map(ns, SITE).transitions)
    assert totals == [(1, 1), (2, 0)]


def test_a_replay_that_ends_on_the_goal_links_the_new_wording(fake_store, ns):
    explore(fake_store, ns)
    planned = begin(fake_store, ns, HRITHIK, goal=REWORDED)
    assert planned.route  # served by the same-words match or the open_url path
    request = FinishRequest(
        site=SITE,
        plan_id=planned.plan_id,
        ok=True,
        goal_text=REWORDED,
        params=HRITHIK,
        goal_reached=True,
        kind="read",
        to_snapshot=article("Hrithik Roshan"),
    )
    session.finish(fake_store, CLOCK, ns, request)
    again = begin(fake_store, ns, SHAH, goal=REWORDED)
    assert again.match == "exact"


def test_an_unknown_plan_is_an_error_only_when_nothing_else_names_the_page(fake_store, ns):
    with pytest.raises(UnknownPlanError):
        session.finish(fake_store, CLOCK, ns, FinishRequest(site=SITE, plan_id="a" * 12, ok=True))
    named = FinishRequest(site=SITE, plan_id="a" * 12, ok=True, to_snapshot=HOME)
    out = session.finish(fake_store, CLOCK, ns, named)
    assert (out.learned, out.goal) == (0, None)


def test_a_blocked_final_page_learns_and_remembers_nothing(fake_store, ns):
    start = begin(fake_store, ns, SHAH)
    wall = Snapshot(url=f"https://{SITE}/captcha", title="Just a moment")
    request = FinishRequest(
        site=SITE,
        from_state=start.state_id,
        steps=(FinishStep(action=click("Go"), to=wall),),
        ok=True,
        goal_text=GOAL,
        params=SHAH,
        goal_reached=True,
        kind="read",
        to_snapshot=wall,
    )
    out = session.finish(fake_store, CLOCK, ns, request)
    assert (out.blocked, out.goal, out.learned) == ("url_path:captcha", None, 0)
    assert begin(fake_store, ns, SHAH).open_url is None


def test_finish_without_a_goal_still_reports_the_page(fake_store, ns):
    out = session.finish(fake_store, CLOCK, ns, FinishRequest(site=SITE, ok=True, to_snapshot=article("X")))
    assert (out.learned, out.goal) == (0, None)
    assert fake_store.get_state(ns, out.state_id) is not None


def test_the_tenants_do_not_share_memos(fake_store, ns):
    explore(fake_store, ns)
    assert begin(fake_store, f"{ns}x", SHAH).open_url is None


def test_the_issued_plan_buffer_expires_and_is_scoped():
    plans = session.IssuedPlans(ttl=-1.0)
    assert plans.get("n", SITE, plans.issue("n", SITE, 1, "k", {}, ())) is None
    fresh = session.IssuedPlans()
    assert fresh.get("n", SITE, fresh.issue("n", SITE, 1, "k", {}, ())) is not None
    assert fresh.get("other", SITE, "x" * 12) is None


def test_memo_helpers_bind_safely():
    assert memo.bind("https://h.test/wiki/{topic}", {"topic": "a b"}) == "https://h.test/wiki/a_b"
    hostile = memo.bind("https://h.test/wiki/{topic}", {"topic": "../../x@evil.test"})
    assert hostile == "https://h.test/wiki/..%2F..%2Fx@evil.test"
    assert memo.bind("https://h.test/wiki/{topic}", {}) is None
    assert memo.clean_url("javascript:alert(1)") is None
    assert memo.clean_url("https://user:pw@h.test/x") is None
    assert memo.clean_url("https://h.test/x#frag") == "https://h.test/x"
