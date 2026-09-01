"""Retry-with-backoff for Copilot/session-layer infra failures (quota, timeout, transient
disconnect) at a draft/audit/fix LLM call site.

Kept separate from a stage's own clarification/verify-cycle budgets (StageSpec.max_cycles /
max_verify_cycles) on purpose: those budgets bound genuine "the model's answer wasn't good enough
yet" attempts, and a Copilot 429/timeout is an infra event, not a bad attempt. Charging it against
those budgets would just move the same "run dies for an infra reason" failure a few laps later
while quietly shrinking the budget available for actual gate-failure fixes. See graph.py's
make_draft_node/make_audit_node and rebuild.py's fix_node for the call sites.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

INFRA_RETRY_ATTEMPTS = int(os.environ.get("AIDW_LLM_INFRA_RETRY_ATTEMPTS", "3"))
# A quota/rate-limit condition does not clear in 0 seconds -- an immediate retry against a still-
# throttled endpoint just burns the attempt budget faster than a short backoff would.
INFRA_RETRY_BACKOFF_SECONDS: tuple[float, ...] = tuple(
    float(s) for s in os.environ.get("AIDW_LLM_INFRA_RETRY_BACKOFF_SECONDS", "5,20,60").split(",") if s.strip()
)


async def call_with_infra_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int = INFRA_RETRY_ATTEMPTS,
    backoff_seconds: tuple[float, ...] = INFRA_RETRY_BACKOFF_SECONDS,
) -> T:
    """Calls fn() (a zero-arg async thunk so callers can close over their real arguments), retrying
    on (TimeoutError, RuntimeError) -- a raw Copilot session failure, not a JSON/schema parse
    failure (ainvoke_structured already retries those itself) -- up to `attempts` times with
    backoff between them, before letting the last exception propagate to the caller."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except (TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            delay = backoff_seconds[min(attempt, len(backoff_seconds) - 1)] if backoff_seconds else 0.0
            logger.warning(
                "%s: infra failure on attempt %d/%d, retrying in %.0fs: %s",
                label, attempt + 1, attempts, delay, exc,
            )
            if delay:
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _demo() -> None:
    """`cd agent && uv run python -m src.infra_retry`."""

    async def _run() -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("simulated quota hit")
            return "ok"

        result = await call_with_infra_retry(flaky, label="demo", attempts=3, backoff_seconds=(0.0,))
        assert result == "ok" and calls["n"] == 2

        async def always_fails() -> str:
            raise TimeoutError("simulated permanent timeout")

        try:
            await call_with_infra_retry(always_fails, label="demo", attempts=2, backoff_seconds=(0.0,))
            raise AssertionError("expected TimeoutError to propagate after exhausting retries")
        except TimeoutError:
            pass

        # A non-infra exception (e.g. a genuine ValueError from schema validation) must never be
        # swallowed or retried -- only (TimeoutError, RuntimeError) are infra-shaped here.
        async def content_error() -> str:
            raise ValueError("not an infra failure")

        try:
            await call_with_infra_retry(content_error, label="demo", attempts=3, backoff_seconds=(0.0,))
            raise AssertionError("expected ValueError to propagate immediately, uncaught")
        except ValueError:
            pass

    asyncio.run(_run())
    print("infra_retry self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
