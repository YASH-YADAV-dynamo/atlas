"""Python SDK (SPEC 3b.4): a thin client over the Route API. Targets (lane P), in sdk/atlas_sdk:

    Atlas(base_url: str, api_key: str | None = None, *, site: str, client: httpx.Client | None = None)
        .session(driver) -> Navigator        # driver: .snapshot() -> dict, .act(action: dict) -> None
    Navigator.run(goal: str, params: dict[str, str] | None = None, *, fallback=None, max_steps=25)
        -> RunSummary(success: bool, steps: int, map_steps: int, fallback_calls: int)
    fallback(snapshot: dict, goal: str, params: dict) -> dict | None
        the developer's own agent step: one action (Route API JSON), or None when the goal is done.
    AtlasApiError(status: int, code: str, message: str)

Snapshots and actions are plain dicts in the Route API's JSON shape. The SDK binds `{slot}`
placeholders and `param` fields with `params` before calling `driver.act`, reports every step,
and calls `fallback` only where the plan says `needs_model`. It must not import the `atlas`
package: it is published on its own as `atlas-sdk`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("atlas_sdk", reason="increment 6: implemented by lane P")
pytest.importorskip("atlas.api.app", reason="needs the Route API")

from atlas_sdk import Atlas, AtlasApiError

from atlas.contracts.types import Action, Snapshot
from tests.support.builders import SITE
from tests.support.fake_site import FakeSite
from tests.support.oracle import OracleModel
from tests.support.scenarios import client_for

SDK_DIR = Path(__file__).resolve().parents[2] / "sdk" / "atlas_sdk"


class DictDriver:
    """The developer's browser, speaking plain dicts."""

    def __init__(self, site: FakeSite) -> None:
        self.site = site

    def snapshot(self) -> dict:
        return self.site.snapshot().model_dump(mode="json")

    def act(self, action: dict) -> None:
        self.site.act(Action.model_validate(action))


class AgentStep:
    """The developer's own agent, used as the fallback."""

    def __init__(self) -> None:
        self.oracle = OracleModel()

    def __call__(self, snapshot: dict, goal: str, params: dict) -> dict | None:
        decision = self.oracle.next_action(goal, params, Snapshot.model_validate(snapshot), ())
        return None if decision.done else decision.action.model_dump(mode="json", exclude_none=True)


def refund(atlas: Atlas, order: str, fallback=None):
    site = FakeSite()
    summary = atlas.session(DictDriver(site)).run(
        f"Refund order {order}", {"order_id": order}, fallback=fallback
    )
    return summary, site


def test_fallback_only_where_the_map_ends(fake_store, ns):
    """R-SDK-1: the first run uses the developer's agent for every step and teaches the map;
    the second run, for another order, never calls it."""
    atlas = Atlas("http://testserver", site=SITE, client=client_for(fake_store, ns))
    agent = AgentStep()
    first, site = refund(atlas, "1042", agent)
    assert (first.success, first.steps, first.map_steps, site.refunded) == (True, 5, 0, {"1042"})
    assert first.fallback_calls == agent.oracle.calls == 6

    agent = AgentStep()
    second, site = refund(atlas, "77", agent)
    assert (second.success, second.steps, second.map_steps) == (True, 5, 5)
    assert (second.fallback_calls, agent.oracle.calls) == (0, 0)
    assert (site.refunded, site.typed) == ({"77"}, ["77"])


def test_without_a_fallback_an_unknown_task_stops_cleanly(fake_store, ns):
    """R-SDK-1: with no fallback and no route the run reports failure and touches nothing."""
    atlas = Atlas("http://testserver", site=SITE, client=client_for(fake_store, ns))
    summary, site = refund(atlas, "5")
    assert (summary.success, summary.steps, summary.fallback_calls, site.acts) == (False, 0, 0, 0)
    assert fake_store.get_map(ns, SITE).transitions == ()


def test_api_errors_are_typed_and_the_key_is_sent(fake_store, ns):
    """R-SDK-2: the SDK sends the bearer key; API errors surface as AtlasApiError with the code."""
    key = "test-key-not-a-secret"
    locked = client_for(fake_store, ns, api_key=key)
    summary, _ = refund(Atlas("http://testserver", key, site=SITE, client=locked), "5")
    assert summary.success is False
    with pytest.raises(AtlasApiError) as caught:
        refund(Atlas("http://testserver", "wrong-key", site=SITE, client=locked), "5")
    assert (caught.value.status, caught.value.code) == (401, "unauthorized")
    with pytest.raises(AtlasApiError) as caught:
        refund(Atlas("http://testserver", key, site="Not A Site", client=locked), "5")
    assert caught.value.status == 422


def test_sdk_is_thin_and_standalone():
    """R-SDK-3: the SDK does not import the atlas package and stays small (about 100 lines)."""
    sources = [path.read_text(encoding="utf-8") for path in SDK_DIR.glob("*.py")]
    assert sources
    for text in sources:
        assert "from atlas." not in text and "import atlas." not in text and "from atlas import" not in text
    code_lines = [
        line
        for text in sources
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(code_lines) <= 180
