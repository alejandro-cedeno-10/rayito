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
#: A receiver refusing the signature or the credential it expects
#: (RFC 9110 §15.5.2 and §15.5.4).
_AUTH_REJECTION_STATUSES: Final = (401, 403)


def is_delivered(status: int) -> bool:
    return status in _SUCCESS_RANGE


def is_retryable(status: int) -> bool:
    """Only a 5xx is worth another attempt: a 3xx/4xx is the receiver's
    deliberate answer (redirects are never followed, T22), and repeating
    the same signed request will not change it."""
    return status in _SERVER_ERROR_RANGE


def is_auth_rejection(status: int) -> bool:
    """A 401/403: worth one more attempt only if the webhook's secret
    turns out to have changed since it was cached."""
    return status in _AUTH_REJECTION_STATUSES


def backoff_seconds(attempt: int) -> float:
    """Wait after the 0-based `attempt` failed, before the next one."""
    return BACKOFF_BASE_SECONDS * (2**attempt)
