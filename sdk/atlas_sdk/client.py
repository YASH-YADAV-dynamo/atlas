"""Route API client and the step loop. One `/v1/start` (locate, match, plan), the known road or the
answer page driven from the response, the caller's own agent (`fallback`) only where the map ends,
and one `/v1/finish` that reports the whole run."""

from __future__ import annotations

import itertools
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

Json = dict[str, Any]

_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
MAX_REPLANS = 2
MAX_FINISH_STEPS = 12
FILLS = ("fill", "select", "check")


class Driver(Protocol):
    def snapshot(self) -> Json: ...

    def act(self, action: Json) -> None: ...


class AtlasApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class RunSummary:
    success: bool
    steps: int
    map_steps: int
    fallback_calls: int
    via: str = field(default="explore", compare=False)  # "url" | "route" | "explore"
    reason: str | None = field(default=None, compare=False)  # why the first plan was or was not served
    state_id: int | None = field(default=None, compare=False)  # the start state


# Apply `text` to every locator name and row, and `value_fn` to the action and each fill.
def _walk(action: Json, text: Callable[[str], str], value_fn: Callable[[Json], Json]) -> Json:

    def locator(loc: Json) -> Json:
        out = {**loc, "name": text(loc.get("name", ""))}
        return {**out, "row": text(loc["row"])} if loc.get("row") is not None else out

    out = value_fn(action)
    if out.get("locator") is not None:
        out["locator"] = locator(out["locator"])
    if action.get("fills"):
        out["fills"] = [{**value_fn(f), "locator": locator(f["locator"])} for f in action["fills"]]
    return out


# Fill `{slot}` placeholders and `param` fields with the caller's values.
def bind_action(action: Json, params: Mapping[str, str]) -> Json:

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

    return _walk(action, text, value)


# Turn parameter values back into slots, so the map learns the road and not the order.
def templatise_action(action: Json, params: Mapping[str, str]) -> Json:
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

    return _walk(action, text, value)


# The action as a standalone form fill (fill, select or check on one control), else None.
def as_fill(action: Json) -> Json | None:
    if action["kind"] not in FILLS or not action.get("locator") or action.get("fills"):
        return None
    fill = {key: action[key] for key in ("locator", "kind", "param", "value") if action.get(key) is not None}
    return fill if "param" in fill or "value" in fill else {**fill, "value": "true"}  # a bare check


# Put the held same-page fills first; a fill the action carries (or is) wins per control.
def fold_fills(action: Json, held: list[Json]) -> Json:
    own = [f["locator"] for f in action.get("fills", ())] + [action.get("locator")]
    mine = [f for f in held if f["locator"] not in own]
    return {**action, "fills": [*mine, *action.get("fills", ())][-20:]} if mine else action


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
        fallback: Callable[[Json, str, Json], Json | None] | None = None,
        max_steps: int = 25,
        kind: str = "act",
    ) -> RunSummary:
        """Follow the known road (or open the remembered answer page) with no model call; ask
        `fallback` one action at a time where the map ends. `kind="read"` marks a lookup."""
        atlas, driver, params = self._atlas, self._driver, dict(params or {})
        task, ids = uuid.uuid4().hex[:12], itertools.count()
        count = {"steps": 0, "map": 0, "fallback": 0}
        snap = driver.snapshot()

        def call(path: str, **body: object) -> Json:
            return atlas.post(path, {"site": atlas.site, "goal_text": goal, "params": params, **body})

        def finish(**body: object) -> Json:
            return call("/v1/finish", kind=kind, event_id=f"{task}-{next(ids)}", to_snapshot=snap, **body)

        def act(action: Json) -> tuple[bool, int]:
            nonlocal snap
            t0, ok, count["steps"] = time.monotonic(), True, count["steps"] + 1
            try:
                driver.act(bind_action(action, params))
                snap = driver.snapshot()
            except Exception:  # any driver failure is reported as a failed step
                ok = False
            return ok, min(int((time.monotonic() - t0) * 1000), 600_000)

        plan = first = call("/v1/start", snapshot=snap)
        via, success, replans, explored = "explore", False, 0, 0
        log: list[Json] = []  # explored steps not yet reported
        held: list[Json] = []  # same-page fills, folded into the action that follows
        state = None  # the explored steps start from the last flushed batch, else the plan's state
        while count["steps"] < max_steps and not success:
            url = plan.get("open_url")  # a remembered answer page: navigate straight there
            road = [{"action": {"kind": "goto", "value": url}}] if url else plan["route"]
            if road and replans < MAX_REPLANS:
                done, total_ms = 0, 0
                for road_step in road[: max_steps - count["steps"]]:
                    ok, ms = act(road_step["action"])
                    count["map"], total_ms = count["map"] + (not url), min(total_ms + ms, 600_000)
                    if not ok:
                        break
                    done += 1
                success = done == len(road)
                finish(
                    plan_id=plan["plan_id"],
                    done=0 if url else done,
                    ok=success,
                    goal_reached=success,
                    ms=total_ms,
                )
                via = ("url" if url else "route") if success else via
                if not success:
                    replans, plan = replans + 1, call("/v1/start", snapshot=snap)  # the frontier
            elif fallback is None:
                break
            else:
                count["fallback"] += 1
                action = fallback(snap, goal, params)
                if action is None:
                    success = explored > 0
                    break
                template, before = templatise_action(action, params), snap["url"]
                ok, ms = act(template)
                fill = as_fill(template)
                if ok and fill is not None and snap["url"] == before:
                    held = [f for f in held if f["locator"] != fill["locator"]] + [fill]
                    continue
                log.append({"action": fold_fills(template, held), "to": snap, "ok": ok, "ms": ms})
                held, explored = [], explored + 1
                if len(log) == MAX_FINISH_STEPS:  # one batch is at most 12 steps: flush, carry on
                    state = finish(from_state=state or plan["state_id"], steps=log, ok=True)["state_id"]
                    log = []
        if log:
            finish(from_state=state or plan["state_id"], steps=log, ok=success, goal_reached=success)
        done = (count["steps"], count["map"], count["fallback"])
        return RunSummary(success, *done, via, first.get("reason"), first["state_id"])
