"""Route API client and the step loop: locate, plan, drive known roads, ask the caller's own
agent (`fallback`) only where the map ends, and report every step."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

type Json = dict[str, Any]
type Fallback = Callable[[Json, str, Json], Json | None]

_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
MAX_REPLANS = 2


class Driver(Protocol):
    """The caller's browser, speaking plain dicts."""

    def snapshot(self) -> Json: ...

    def act(self, action: Json) -> None: ...


class AtlasApiError(Exception):
    """A non-200 answer from the Route API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class RunSummary:
    success: bool
    steps: int
    map_steps: int
    fallback_calls: int


def _locator(loc: Json, convert: Callable[[str], str]) -> Json:
    out = {**loc, "name": convert(loc.get("name", ""))}
    if loc.get("row") is not None:
        out["row"] = convert(loc["row"])
    return out


def _walk(action: Json, locator_fn: Callable[[Json], Json], value_fn: Callable[[Json], Json]) -> Json:
    """Apply `locator_fn` to every locator and `value_fn` to the action and each fill."""
    out = value_fn(action)
    if out.get("locator") is not None:
        out["locator"] = locator_fn(out["locator"])
    if action.get("fills"):
        out["fills"] = [{**value_fn(f), "locator": locator_fn(f["locator"])} for f in action["fills"]]
    return out


def bind_action(action: Json, params: Mapping[str, str]) -> Json:
    """Fill `{slot}` placeholders and `param` fields with the caller's values."""

    def value(item: Json) -> Json:
        out = {key: val for key, val in item.items() if key != "param"}
        if item.get("param") is not None:
            out["value"] = params[item["param"]]
        elif isinstance(item.get("value"), str) and item.get("kind") != "goto":
            # a typed value may hold a slot: "{topic} awards"
            out["value"] = _SLOT.sub(lambda m: params.get(m.group(1), m.group(0)), item["value"])
        return out

    def text(t: str) -> str:
        return _SLOT.sub(lambda m: params[m.group(1)], t)

    return _walk(action, lambda loc: _locator(loc, text), value)


def templatise_action(action: Json, params: Mapping[str, str]) -> Json:
    """Turn parameter values back into slots, so the map learns the road and not the order."""
    slots = {val: slot for slot, val in sorted(params.items(), reverse=True) if val}
    words = sorted(slots, key=len, reverse=True)
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(w)}(?!\w)" for w in words) or "(?!)")

    def value(item: Json) -> Json:
        if item.get("value") in slots:
            out = {key: val for key, val in item.items() if key != "value"}
            return {**out, "param": slots[item["value"]]}
        return dict(item)

    def text(t: str) -> str:
        return pattern.sub(lambda m: "{" + slots[m.group(0)] + "}", t)

    return _walk(action, lambda loc: _locator(loc, text), value)


def _has_slots(action: Json) -> bool:
    """True when the action is bound from the task's parameters."""
    found = False

    def note(item: Json) -> Json:
        nonlocal found
        found = found or bool(item.get("param"))
        return item

    def locator(loc: Json) -> Json:
        nonlocal found
        found = found or "{" in str(loc.get("name", "")) or "{" in str(loc.get("row") or "")
        return loc

    _walk(action, locator, note)
    return found


class Atlas:
    def __init__(
        self, base_url: str, api_key: str | None = None, *, site: str, client: httpx.Client | None = None
    ) -> None:
        self.base_url, self.site = base_url.rstrip("/"), site
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = client or httpx.Client(timeout=30.0)

    def post(self, path: str, body: Json) -> Json:
        response = self._http.post(f"{self.base_url}{path}", json=body, headers=self._headers)
        if response.status_code != 200:
            try:
                data = response.json()
                code, message = str(data["code"]), str(data.get("message", ""))
            except (ValueError, KeyError, TypeError):
                code, message = "invalid_request", response.text[:200]
            raise AtlasApiError(response.status_code, code, message)
        return response.json()

    def session(self, driver: Driver) -> Navigator:
        return Navigator(self, driver)


class Navigator:
    def __init__(self, atlas: Atlas, driver: Driver) -> None:
        self._atlas, self._driver = atlas, driver

    def run(
        self,
        goal: str,
        params: Mapping[str, str] | None = None,
        *,
        fallback: Fallback | None = None,
        max_steps: int = 25,
    ) -> RunSummary:
        atlas, driver, params = self._atlas, self._driver, dict(params or {})
        task = uuid.uuid4().hex[:12]
        counts = {"steps": 0, "map": 0, "fallback": 0}
        last: Json | None = None
        snap = driver.snapshot()

        def step(state: int, action: Json, expect: int | None) -> tuple[int, bool]:
            nonlocal snap, last
            t0 = time.monotonic()
            try:
                driver.act(bind_action(action, params))
                ok = True
            except Exception:  # any driver failure is reported as a failed step
                ok = False
            ms = min(int((time.monotonic() - t0) * 1000), 600_000)
            if ok:
                snap = driver.snapshot()
            last = {
                "site": atlas.site, "from_state": state, "action": action, "to_snapshot": snap,
                "ok": ok, "ms": ms, "event_id": f"{task}-s{counts['steps']}",
                "goal_text": goal, "params": params,
            }  # fmt: skip
            if ok and expect is not None:
                last["expect_state"] = expect
            counts["steps"] += 1
            reply = atlas.post("/v1/report", last)
            return (reply["state_id"] if ok else state), ok

        state = atlas.post("/v1/locate", {"site": atlas.site, "snapshot": snap, "params": params})["state_id"]
        replans, success = 0, False
        while counts["steps"] < max_steps and not success:
            body = {"site": atlas.site, "state_id": state, "goal_text": goal, "params": params}
            plan = atlas.post("/v1/plan", {**body, "snapshot": snap})
            if plan["route"] and replans < MAX_REPLANS:
                for index, route_step in enumerate(plan["route"]):
                    if counts["steps"] >= max_steps:
                        break
                    expect = route_step["expect_state"]
                    state, ok = step(state, route_step["action"], expect)
                    counts["map"] += 1
                    final = index == len(plan["route"]) - 1
                    if ok and final and state != expect and _has_slots(route_step["action"]):
                        # A parameterised last step lands on a page for this task's values (another
                        # article, another order): that is the goal, not a wrong turn.
                        success = True
                        break
                    if not ok or state != expect:
                        replans += 1
                        break
                    success = final
                continue
            if fallback is None:
                break
            counts["fallback"] += 1
            action = fallback(snap, goal, params)
            if action is None:
                success = last is not None
                break
            state, _ = step(state, templatise_action(action, params), None)
        if success and last is not None:
            atlas.post("/v1/report", {**last, "goal_reached": True})
        return RunSummary(success, counts["steps"], counts["map"], counts["fallback"])
