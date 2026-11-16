"""Bounded retry for transient store errors (R-RETRY-1).

Only `StoreUnavailableError` is retried: the adapter maps driver "service unavailable", timeouts
and connection resets to it. Query, format and programming errors surface at once. Retrying is
safe because every store write is an idempotent MERGE/SET keyed by id (ADR-0001).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from atlas.contracts.errors import StoreUnavailableError

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn` up to `attempts` times, sleeping `base_delay * 2**n` after the n-th failure.

    The last `StoreUnavailableError` is re-raised once the attempts are spent.
    """
    for attempt in range(attempts - 1):
        try:
            return fn()
        except StoreUnavailableError:
            sleep(base_delay * 2**attempt)
    return fn()
