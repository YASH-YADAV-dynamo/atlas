"""Landing rule (SHIP_PLAN 4.2 fix 2, H2): a parameterised step that lands on the page of another
subject is a success of the followed edge, not a failure plus a new state, edge and goal link.
Targets atlas.core.learner.report."""

from __future__ import annotations

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.types import Action, ActionKind, Element, Fill, Locator, Snapshot
from atlas.core.planner import plan
from tests.support.builders import learn, state_id_of

WIKI = "en.wikipedia.org"
GOAL = "Open the Wikipedia article about {topic}"
BOX = Locator(role="searchbox", name="Search Wikipedia")
SEARCH = Action(kind=ActionKind.PRESS, locator=BOX, value="Enter", fills=(Fill(locator=BOX, param="topic"),))
HOME = Snapshot(
    url=f"https://{WIKI}/wiki/Main_Page",
    elements=(Element(role="searchbox", name="Search Wikipedia"), Element(role="link", name="Contents")),
)


def article(topic: str) -> Snapshot:
    links = (f"{topic} filmography", f"{topic} awards", "Edit", "Talk")
    return Snapshot(
        url=f"https://{WIKI}/wiki/{topic.replace(' ', '_')}",
        title=f"{topic} - Wikipedia",
        elements=tuple(Element(role="link", name=n) for n in links),
    )


def search(store, ns, topic: str, expect: int | None, goal_reached: bool = True):
    bound = SEARCH.model_copy(update={"fills": (Fill(locator=BOX, value=topic),)})
    return learn(
        store,
        ns,
        HOME,
        bound,
        article(topic),
        site=WIKI,
        params={"topic": topic},
        expect_state=expect,
        goal_text=GOAL.format(topic=topic),
        goal_reached=goal_reached,
    )


def test_each_new_subject_reuses_the_edge_and_records_no_failure(fake_store, ns):
    first = search(fake_store, ns, "Hrithik Roshan", None)
    expect = first.state_id
    for topic in ("Shah Rukh Khan", "Deepika Padukone", "Kajol"):
        result = search(fake_store, ns, topic, expect)
        assert result.state_id == expect
    view = fake_store.get_map(ns, WIKI)
    assert len(view.states) == 2 and len(view.transitions) == 1
    assert (view.transitions[0].ok, view.transitions[0].fail) == (4, 0)
    home = state_id_of(fake_store, ns, HOME, site=WIKI)
    request = PlanRequest(site=WIKI, state_id=home, goal_text=GOAL, params={"topic": "X"})
    planned = plan(fake_store, ns, request)
    assert planned.route and planned.route[0].reliability > 0.8


def test_a_parameterised_step_to_another_template_is_still_a_failure(fake_store, ns):
    expect = search(fake_store, ns, "Kajol", None).state_id
    elsewhere = Snapshot(url=f"https://{WIKI}/wiki/Special:Search", elements=HOME.elements[1:])
    bound = SEARCH.model_copy(update={"fills": (Fill(locator=BOX, value="Nobody"),)})
    result = learn(
        fake_store, ns, HOME, bound, elsewhere, site=WIKI, params={"topic": "Nobody"}, expect_state=expect
    )
    assert result.state_id != expect
    failed = [t for t in fake_store.get_map(ns, WIKI).transitions if t.to_state == expect]
    assert (failed[0].ok, failed[0].fail) == (1, 1)
