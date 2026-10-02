"""The deliverer's retry policy (§7.4: "retries at most 3 times with
backoff"), pure: which HTTP outcomes count as delivered, which are worth
another attempt, and how long to wait in between.
"""

from __future__ import annotations

from typing import Final

#: §7.4: at most 3 attempts per (event, webhook) and invocation.
MAX_ATTEMPTS: Final = 3
#: Doubles each attempt: 0.5 s, then 1 s.
BACKOFF_BASE_SECONDS: Final = 0.5

_SUCCESS_RANGE: Final = range(200, 300)
_SERVER_ERROR_RANGE: Final = range(500, 600)


def is_delivered(status: int) -> bool:
    return status in _SUCCESS_RANGE


def is_retryable(status: int) -> bool:
    """Only a 5xx is worth another attempt: a 3xx/4xx is the receiver's
    deliberate answer (redirects are never followed, T22), and repeating
    the same signed request will not change it."""
    return status in _SERVER_ERROR_RANGE


def backoff_seconds(attempt: int) -> float:
    """Wait after the 0-based `attempt` failed, before the next one."""
    return BACKOFF_BASE_SECONDS * (2**attempt)
