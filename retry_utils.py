"""
LOCKBOT Network Retry Utility v1.0

A small, reusable retry wrapper for calls to the Alpaca API (or any
flaky network call). Addresses the network-loss resilience gap: right
now, if wifi drops mid-cycle, an Alpaca call raises an exception
immediately and the whole component fails that attempt — which the
controller's existing crash-recovery *does* catch, but only after
burning a full retry attempt and a 10-second delay for something that
might have resolved itself in a couple of seconds.

This does NOT change what LOCKBOT does — it only makes transient
network hiccups less likely to cost a full component failure. It does
not retry on things that should fail immediately (like invalid
credentials or a rejected order) — see RETRYABLE_EXCEPTIONS below.

Usage example (wiring this in is optional, applied case by case):

    from retry_utils import with_retries

    account = with_retries(trading_client.get_account)()

Or as a decorator on a function you define yourself:

    @retry_on_network_error()
    def fetch_account(trading_client):
        return trading_client.get_account()
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, TypeVar

import requests

T = TypeVar("T")

# Network-level failures worth retrying. Deliberately narrow — this
# should NOT catch things like invalid API keys, rejected orders, or
# validation errors, which should surface immediately rather than
# retry uselessly.
RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 2.0


def with_retries(
    func: Callable[..., T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
) -> Callable[..., T]:
    """
    Wrap a callable so transient network errors are retried with
    exponential backoff before giving up and re-raising.

    Non-network exceptions (bad credentials, invalid order parameters,
    etc.) are never retried — they raise immediately on the first
    attempt, exactly as they would without this wrapper.
    """

    @functools.wraps(func)
    def wrapped(*args: Any, **kwargs: Any) -> T:
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                return func(*args, **kwargs)

            except RETRYABLE_EXCEPTIONS as error:
                last_error = error

                if attempt == max_attempts:
                    break

                wait_seconds = base_delay_seconds * (2 ** (attempt - 1))

                print(
                    f"Network error calling {func.__name__} "
                    f"(attempt {attempt}/{max_attempts}): "
                    f"{type(error).__name__}: {error}. "
                    f"Retrying in {wait_seconds:.1f} seconds..."
                )

                time.sleep(wait_seconds)

        raise last_error  # type: ignore[misc]

    return wrapped


def retry_on_network_error(
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator form of with_retries, for wrapping your own functions."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        return with_retries(
            func,
            max_attempts=max_attempts,
            base_delay_seconds=base_delay_seconds,
        )

    return decorator


if __name__ == "__main__":
    # Quick self-test using a fake flaky function — no network calls.
    attempts_made = {"count": 0}

    def flaky_function() -> str:
        attempts_made["count"] += 1

        if attempts_made["count"] < 2:
            raise requests.exceptions.ConnectionError("simulated network drop")

        return "success"

    result = with_retries(flaky_function, base_delay_seconds=0.1)()
    print(f"Result: {result} (took {attempts_made['count']} attempt(s))")
    print("Status: READY")
