"""Retry on transient store errors (wave 2). Targets
    atlas.adapters.hydradb.retry.call_with_retry(fn: Callable[[], T], *, attempts: int = 3,
        base_delay: float = 0.05, sleep: Callable[[float], None] = time.sleep) -> T
Retries only StoreUnavailableError, with delays base_delay * 2**n between attempts, and
re-raises the last one. Safe because every store write is idempotent (ADR-0001).
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.adapters.hydradb.retry", reason="wave 2: implemented by lane C")

from atlas.adapters.hydradb.retry import call_with_retry
from atlas.contracts.errors import PathFormatError, StoreUnavailableError


class Flaky:
    def __init__(self, failures: int, error: Exception) -> None:
        self.failures, self.error, self.calls = failures, error, 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "done"


def test_transient_errors_are_retried_with_backoff():
    """R-RETRY-1: a call that fails twice with StoreUnavailableError succeeds on the third try."""
    naps: list[float] = []
    flaky = Flaky(2, StoreUnavailableError("down"))
    assert call_with_retry(flaky, attempts=3, base_delay=0.1, sleep=naps.append) == "done"
    assert flaky.calls == 3
    assert naps == pytest.approx([0.1, 0.2])


def test_retries_are_bounded_and_the_error_is_kept():
    """R-RETRY-1: after `attempts` tries the typed error is raised, not swallowed."""
    naps: list[float] = []
    flaky = Flaky(10, StoreUnavailableError("still down"))
    with pytest.raises(StoreUnavailableError, match="still down"):
        call_with_retry(flaky, attempts=4, base_delay=0.01, sleep=naps.append)
    assert (flaky.calls, len(naps)) == (4, 3)


@pytest.mark.parametrize("error", [PathFormatError("bad"), ValueError("bug")])
def test_other_errors_are_not_retried(error):
    """R-RETRY-1: only transient store errors are retried; everything else surfaces at once."""
    flaky = Flaky(1, error)
    with pytest.raises(type(error)):
        call_with_retry(flaky, sleep=lambda _: None)
    assert flaky.calls == 1
