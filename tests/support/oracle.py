"""Test doubles for two ports used by the benchmark and runner tests.

OracleModel: a ModelClient that knows the refund flow (and its renamed variant) of
tests/support/fake_site.py, so
"model-driven" arms are deterministic. It reports 100 input and 10 output tokens per call.
LocalRouteApi: the RouteApi port served in-process by core on a given store.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from atlas.contracts.api_models import (
    LocateRequest,
    LocateResponse,
    PlanRequest,
    PlanResponse,
    ReportRequest,
    ReportResponse,
)
from atlas.contracts.ports import GraphStore
from atlas.contracts.types import Action, ActionKind, Fill, Locator, ModelDecision, Params, Snapshot
from tests.support.builders import FixedClock

INPUT_TOKENS = 100
OUTPUT_TOKENS = 10


def _click(role: str, name: str, **extra) -> Action:
    return Action(
        kind=ActionKind.CLICK, locator=Locator(role=role, name=name, row=extra.pop("row", None)), **extra
    )


class OracleModel:
    def __init__(self) -> None:
        self.calls = 0

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        self.calls += 1
        order = params["order_id"]
        url = urlsplit(snapshot.url)
        names = {element.name for element in snapshot.elements}
        if url.path in ("/", "/settings"):
            action = _click("link", "Orders")
        elif url.path == "/orders" and not url.query:
            search = Fill(locator=Locator(role="textbox", name="Search orders"), value=order)
            action = _click("button", "Search", fills=(search,))
        elif url.path == "/orders":
            action = _click("link", "View order", row=order)
        elif "Confirm refund" in names:
            action = _click("button", "Confirm refund")
        elif {"Refund order", "Issue refund"} & names:
            action = _click("link", "Issue refund" if "Issue refund" in names else "Refund order")
        else:
            return ModelDecision(
                action=None, done=True, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
            )
        return ModelDecision(
            action=action, done=False, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
        )


class LocalRouteApi:
    def __init__(self, store: GraphStore, ns: str) -> None:
        self._store, self._ns, self._clock = store, ns, FixedClock()
        self.calls: list[str] = []

    def locate(self, request: LocateRequest) -> LocateResponse:
        from atlas.core.locate import locate

        self.calls.append("locate")
        return LocateResponse(**locate(self._store, self._ns, request.site, request.snapshot).model_dump())

    def plan(self, request: PlanRequest) -> PlanResponse:
        from atlas.core.planner import plan

        self.calls.append("plan")
        return PlanResponse(**plan(self._store, self._ns, request).model_dump())

    def report(self, request: ReportRequest) -> ReportResponse:
        from atlas.core.learner import report

        self.calls.append("report")
        result = report(self._store, self._clock, self._ns, request)
        return ReportResponse(state_id=result.state_id, known=result.known, duplicate=result.duplicate)
