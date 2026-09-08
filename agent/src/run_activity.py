"""Process-local, in-memory, reference-counted "is a run actually executing right now" signal,
plus a cross-process heartbeat file for the one caller that lives in a different OS process.

Mirrors sandbox/registry.py's module-level-dict pattern (SPECIFICATION.md Decision 4: small
internal tool, don't over-engineer). Refcounted, not boolean: an overlapping reattach/duplicate
run on the same session id must not have one finishing clear the other's active flag.

# ponytail: process-local refcount, single-instance only -- needs a shared store (Redis/DB row)
if this ever runs multi-worker; not needed today (docker-entrypoint.sh runs uvicorn with no
--workers flag), same caveat registry.py and checkpoint.py's AsyncSqliteSaver already carry.

run_headless.py drives the identical graph.py code in a wholly separate OS process, so the
in-memory _counts dict above is invisible to it -- incr()/decr() from that process would just
update a dict nobody else ever reads. is_active() therefore also checks a heartbeat FILE
(_HEARTBEAT_DIR), touched periodically by whichever external process is working a session via
heartbeat(). Deliberately not a PID-liveness check (contrast run_lock.py's _pid_alive): a killed
process never runs its cleanup, and a later-reused PID would then read as "still active" forever
-- worse than the bug being fixed, since it would permanently hide a real crash. A periodic touch
that goes stale after a few missed beats has no such failure mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import AsyncIterator

_counts: dict[str, int] = {}

# Per-thread lock serializing actual graph execution (main.py's _ReattachStateAgent.run()) --
# separate from _counts above, which only ever tracked concurrent attaches for DISPLAY. Two tabs
# reattaching/resubmitting to the same thread_id used to both call graph.astream concurrently: a
# real risk of doubled side effects (duplicate git ops, duplicate PR opens) and checkpoint-write
# races. setdefault, not a plain dict literal per thread_id: multiple concurrent first-callers for
# a never-before-seen thread_id must all resolve to the SAME Lock instance, not one each.
_locks: dict[str, asyncio.Lock] = {}

_HEARTBEAT_DIR = Path(__file__).resolve().parents[1] / "data" / "run_active"
_BEAT_INTERVAL = 8  # seconds between touches
_STALE_AFTER = 25  # seconds -- ~3 missed beats of margin before a marker reads as dead


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
    key = session_id.lower()
    if _counts.get(key, 0) > 0:
        return True
    marker = _HEARTBEAT_DIR / f"{key}.beat"
    try:
        age = time.time() - marker.stat().st_mtime
    except FileNotFoundError:
        return False
    if age <= _STALE_AFTER:
        return True
    # Stale: whatever process was beating this is gone (or wedged). Clean up in passing rather
    # than waiting for that process to do it -- it never will, if it crashed.
    with contextlib.suppress(FileNotFoundError):
        marker.unlink()
    return False


def get_lock(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id.lower(), asyncio.Lock())


@contextlib.asynccontextmanager
async def heartbeat(session_id: str) -> AsyncIterator[None]:
    """Keeps `is_active(session_id)` true for as long as the `async with` block runs, from any
    process. For same-process callers (main.py), incr()/decr() already do this with zero latency
    and zero disk I/O -- this is for a caller (run_headless.py) whose in-memory refcount nobody
    else can see.
    """
    key = session_id.lower()
    _HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    marker = _HEARTBEAT_DIR / f"{key}.beat"

    async def _beat() -> None:
        while True:
            marker.touch()
            await asyncio.sleep(_BEAT_INTERVAL)

    marker.touch()
    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(FileNotFoundError):
            marker.unlink()


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.run_activity`."""
    import tempfile

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

    # Cross-process heartbeat: is_active must reflect a fresh beat file with no local refcount,
    # then flip false once the beat goes stale -- and clean the stale file up in passing.
    global _HEARTBEAT_DIR, _BEAT_INTERVAL
    real_dir, real_interval = _HEARTBEAT_DIR, _BEAT_INTERVAL
    with tempfile.TemporaryDirectory() as tmp:
        _HEARTBEAT_DIR = Path(tmp)
        try:
            marker = _HEARTBEAT_DIR / "heartbeat-check.beat"
            marker.touch()
            assert is_active("heartbeat-check") is True, "fresh beat file must count as active"

            stale = time.time() - _STALE_AFTER - 5
            os.utime(marker, (stale, stale))
            assert is_active("heartbeat-check") is False, "beat older than _STALE_AFTER must be stale"
            assert not marker.exists(), "is_active must remove a stale marker it encounters"

            # heartbeat(): actually keeps beating for the life of the `async with` block, and
            # cleans its file up on exit -- checked with a fast interval so this stays instant.
            _BEAT_INTERVAL = 0.05

            async def _uses_heartbeat() -> None:
                async with heartbeat("live-check"):
                    await asyncio.sleep(0.2)
                    assert is_active("live-check") is True, "heartbeat() must keep the marker fresh"

            asyncio.run(_uses_heartbeat())
            assert is_active("live-check") is False, "heartbeat() must remove its marker on exit"
        finally:
            _HEARTBEAT_DIR, _BEAT_INTERVAL = real_dir, real_interval

    print("run_activity self-check passed")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_activity
    _demo()
