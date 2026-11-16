"""C5 (SHIP_PLAN 4.3): the Playwright driver waits for network idle (3 s cap), then re-reads the page
until two consecutive snapshots have the same element count (250 ms apart, at most 3 reads)."""

from __future__ import annotations

import pytest

pytest.importorskip("atlas_sdk")

from atlas_sdk.drivers.playwright import PlaywrightDriver


class FakeBody:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def aria_snapshot(self) -> str:
        self.page.reads += 1
        counts = self.page.counts
        count = counts[min(self.page.reads, len(counts)) - 1]
        return "\n".join(f'- link "Item {i}"' for i in range(count))

    def inner_text(self) -> str:
        return "body"


class FakePage:
    """Each read of the page shows the next element count from `counts` (the last one repeats)."""

    url = "https://example.test/start"
    main_frame = object()

    def __init__(self, counts: list[int], idle_fails: bool = False) -> None:
        self.counts, self.reads, self.idle_fails = counts, 0, idle_fails
        self.waits: list[tuple] = []

    def on(self, *_args: object) -> None:
        return None

    def title(self) -> str:
        return "Start"

    def locator(self, _selector: str) -> FakeBody:
        return FakeBody(self)

    def evaluate(self, _script: str) -> None:
        return None

    def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.waits.append(("load_state", state, timeout))
        if self.idle_fails:
            raise TimeoutError("network never idle")

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(("timeout", ms))


def snap_with(counts: list[int], **kw) -> tuple[dict, FakePage]:
    page = FakePage(counts, **kw)
    return PlaywrightDriver(page).snapshot(), page


def test_waits_for_network_idle_with_a_three_second_cap():
    _, page = snap_with([4])
    assert page.waits[0] == ("load_state", "networkidle", 3000)


def test_two_agreeing_reads_stop_the_wait():
    snap, page = snap_with([4, 4, 9])
    assert (page.reads, len(snap["elements"])) == (2, 4)
    assert [w for w in page.waits if w[0] == "timeout"] == [("timeout", 250)]


def test_a_late_render_is_waited_for():
    snap, page = snap_with([3, 8, 8])
    assert (page.reads, len(snap["elements"])) == (3, 8)


def test_a_page_that_never_settles_is_read_three_times_at_most():
    snap, page = snap_with([1, 2, 3, 4, 5])
    assert (page.reads, len(snap["elements"])) == (3, 3)


def test_network_idle_timeout_is_not_fatal():
    snap, page = snap_with([5, 5], idle_fails=True)
    assert len(snap["elements"]) == 5 and page.reads == 2
