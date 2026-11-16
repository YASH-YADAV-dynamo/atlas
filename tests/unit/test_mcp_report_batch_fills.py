"""Batched reports fold form fills inside one call (SHIP_PLAN 4.3 C7/C8)."""
# ruff: noqa: F811, F401

from __future__ import annotations

from atlas.contracts import mcp_models as m
from atlas.contracts.types import Action, ActionKind, Locator
from atlas.mcp.pending import PENDING
from tests.unit.test_mcp_plan_replay import (
    GOAL,
    HOME,
    SITE,
    article,
    ask,
    results,
    tools,
)

BOX = Locator(role="searchbox", name="Search")
GO = Action(kind=ActionKind.CLICK, locator=Locator(role="button", name="Go"))


def fill(value: str) -> Action:
    return Action(kind=ActionKind.FILL, locator=BOX, value=value)


def pending_count() -> int:
    return len(PENDING._items)  # pyright: ignore[reportPrivateUsage]


def test_standalone_fill_is_folded_in_the_same_call_without_the_memory_buffer(tools):
    before = pending_count()
    p = ask(tools, "Ann")
    pick = Action(kind=ActionKind.CLICK, locator=Locator(role="link", name="Ann"))
    tools.report(
        m.ReportInput(
            site=SITE,
            from_state=p.state_id,
            ok=True,
            goal_text=GOAL,
            params={"topic": "Ann"},
            goal_reached=True,
            kind="read",
            steps=(
                m.ReportStep(action=fill("Ann"), to=HOME),
                m.ReportStep(action=GO, to=results("Ann")),
                m.ReportStep(action=pick, to=article("Ann")),
            ),
        )
    )
    assert pending_count() == before
    again = ask(tools, "Bob", replay="script")
    assert again.script and '.fill("Bob")' in again.script  # the fill replays before the click


def test_a_later_fill_of_the_same_control_wins(tools):
    p = ask(tools, "Ann")
    out = tools.report(
        m.ReportInput(
            site=SITE,
            from_state=p.state_id,
            ok=True,
            steps=(
                m.ReportStep(action=fill("typo"), to=HOME),
                m.ReportStep(action=fill("Ann"), to=HOME),
                m.ReportStep(action=GO, to=results("Ann")),
            ),
        )
    )
    assert out.learned == 1  # two fills and a click are one stored step
