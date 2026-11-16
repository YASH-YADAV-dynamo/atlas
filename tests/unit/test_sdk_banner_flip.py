"""The live-test regression (SHIP_PLAN 4, H1 and H2): a start page whose campaign banner comes and
goes between visits, and article pages whose links differ per subject, must not stop reuse.
After the first topic, every run follows the map with zero model calls."""

from __future__ import annotations

import pytest

pytest.importorskip("atlas_sdk")

from atlas_sdk import Atlas

from tests.support.scenarios import client_for

SITE = "en.wikipedia.org"
GOAL = "Open the Wikipedia article about {topic}"
TOPICS = ("Hrithik Roshan", "Shah Rukh Khan", "Deepika Padukone", "Ranbir Kapoor", "Hrithik Roshan", "Kajol")
PORTALS = [f"Portal {name}" for name in ("Arts", "Biography", "Geography", "History", "Mathematics")]
CONTROLS = [
    ("searchbox", "Search Wikipedia"),
    ("button", "Search"),
    *[("link", name) for name in ("Main page", "Contents", "Current events", "Random article", "About")],
    *[("link", name) for name in ("Contact us", "Help", "Donate", "Create account", "Log in", *PORTALS)],
]
BANNER = [("link", "Wiki Loves Folklore: take part in the photo contest"), ("button", "hide")]


class WikiDriver:
    """Main_Page toggles a banner on every visit; an article lists links of its own subject."""

    visits = 0

    def __init__(self) -> None:
        WikiDriver.visits += 1
        self.banner = WikiDriver.visits % 2 == 0
        self.url = f"https://{SITE}/wiki/Main_Page"
        self.title = "Wikipedia, the free encyclopedia"
        self.query = ""

    def snapshot(self) -> dict:
        if self.url.endswith("Main_Page"):
            pairs = CONTROLS + (BANNER if self.banner else [])
        else:
            topic = self.title.removesuffix(" - Wikipedia")
            own = [("link", f"{topic} filmography"), ("link", f"Awards received by {topic}")]
            pairs = [*CONTROLS[:2], ("link", "Edit"), ("link", "Talk"), *own]
        elements = [{"role": role, "name": name} for role, name in pairs]
        return {"url": self.url, "title": self.title, "elements": elements}

    def act(self, action: dict) -> None:
        assert action["kind"] == "press" and action["locator"]["name"] == "Search Wikipedia"
        topic = action["fills"][0]["value"]
        assert "{" not in topic
        self.url = f"https://{SITE}/wiki/{topic.replace(' ', '_')}"
        self.title = f"{topic} - Wikipedia"


class Agent:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, snapshot: dict, goal: str, params: dict) -> dict | None:
        self.calls += 1
        if not snapshot["url"].endswith("Main_Page"):
            return None  # the article is open
        box = {"role": "searchbox", "name": "Search Wikipedia"}
        fill = {"locator": box, "value": params["topic"]}
        return {"kind": "press", "locator": box, "value": "Enter", "fills": [fill]}


def test_banner_flip_gives_zero_model_calls_on_every_repeat(fake_store, ns):
    atlas = Atlas("http://testserver", site=SITE, client=client_for(fake_store, ns))
    WikiDriver.visits = 0
    calls = []
    for topic in TOPICS:
        agent, driver = Agent(), WikiDriver()
        summary = atlas.session(driver).run(GOAL, {"topic": topic}, fallback=agent)
        assert summary.success, topic
        assert driver.title == f"{topic} - Wikipedia"
        calls.append(agent.calls)
    assert calls == [2, 0, 0, 0, 0, 0]
    view = fake_store.get_map(ns, SITE)
    assert len(view.states) == 2  # Main_Page (one state, banner or not) and /wiki/{topic}
    assert [(t.ok, t.fail) for t in view.transitions] == [(len(TOPICS), 0)]
