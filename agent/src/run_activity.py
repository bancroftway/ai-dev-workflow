"""Process-local, in-memory, reference-counted "is a run actually executing right now" signal.

Mirrors sandbox/registry.py's module-level-dict pattern (SPECIFICATION.md Decision 4: small
internal tool, don't over-engineer). Refcounted, not boolean: an overlapping reattach/duplicate
run on the same session id must not have one finishing clear the other's active flag.

# ponytail: process-local refcount, single-instance only -- needs a shared store (Redis/DB row)
if this ever runs multi-worker; not needed today (docker-entrypoint.sh runs uvicorn with no
--workers flag), same caveat registry.py and checkpoint.py's AsyncSqliteSaver already carry.
"""

from __future__ import annotations

import asyncio

_counts: dict[str, int] = {}

# Per-thread lock serializing actual graph execution (main.py's _ReattachStateAgent.run()) --
# separate from _counts above, which only ever tracked concurrent attaches for DISPLAY. Two tabs
# reattaching/resubmitting to the same thread_id used to both call graph.astream concurrently: a
# real risk of doubled side effects (duplicate git ops, duplicate PR opens) and checkpoint-write
# races. setdefault, not a plain dict literal per thread_id: multiple concurrent first-callers for
# a never-before-seen thread_id must all resolve to the SAME Lock instance, not one each.
_locks: dict[str, asyncio.Lock] = {}


def incr(session_id: str) -> None:
    key = session_id.lower()
    _counts[key] = _counts.get(key, 0) + 1


def decr(session_id: str) -> None:
    key = session_id.lower()
    count = _counts.get(key, 0) - 1
    if count > 0:
        _counts[key] = count
    else:
        _counts.pop(key, None)


def is_active(session_id: str) -> bool:
    return _counts.get(session_id.lower(), 0) > 0


def get_lock(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id.lower(), asyncio.Lock())


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.run_activity`."""
    # Case-insensitive keys: the frontend mints lowercase UUIDs (crypto.randomUUID()) but SQL
    # Server round-trips UNIQUEIDENTIFIER uppercase (same reason session_store/run_event_store
    # normalize to lowercase on read) -- normalize here too so neither caller has to remember to.
    incr("ABC-123")
    assert is_active("abc-123") is True

    # Overlapping runs on the same id (reattach racing a still-finishing prior stream): one
    # decr() must not clear the other's active flag.
    incr("abc-123")
    decr("abc-123")
    assert is_active("abc-123") is True, "second incr() must survive the first decr()"

    # Decrement-to-zero cleanup: is_active flips false, and the dict doesn't leak the key.
    decr("abc-123")
    assert is_active("abc-123") is False
    assert "abc-123" not in _counts, "count should be popped at zero, not left at 0"

    # Extra/redundant decr() past zero is a safe no-op, never negative, never raises.
    decr("abc-123")
    assert is_active("abc-123") is False

    # get_lock: same session_id (any casing) must resolve to the SAME Lock instance -- two tabs
    # racing to attach to a never-before-seen thread must still serialize against each other, not
    # each get their own independent lock.
    assert get_lock("XYZ-999") is get_lock("xyz-999")

    async def _lock_serializes() -> None:
        lock = get_lock("lock-check")
        order: list[str] = []

        async def holder() -> None:
            async with lock:
                order.append("holder-acquired")
                await asyncio.sleep(0.05)
                order.append("holder-released")

        async def waiter() -> None:
            await asyncio.sleep(0.01)  # let holder acquire first
            async with lock:
                order.append("waiter-acquired")

        await asyncio.gather(holder(), waiter())
        assert order == ["holder-acquired", "holder-released", "waiter-acquired"], order

    asyncio.run(_lock_serializes())

    print("run_activity self-check passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_activity
    _demo()
