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

Also owns the per-thread background graph task registry and its event-subscriber fan-out (the SSE
disconnect fix): main.py's _ReattachStateAgent.run() no longer drives graph.astream_events()
inside the HTTP request coroutine -- it spawns a detached asyncio.Task (_drive_graph) tracked here,
and each attached browser tab's HTTP generator is just a subscriber reading that task's published
events off its own bounded queue. A tab disconnecting only drops its queue; the task keeps running.
Colocated here rather than in main.py so sessions_api.py (terminate_session's Stop button) can
reach cancel_run() without importing main.py (circular), and so this module's existing _demo()
self-check convention covers the new logic too instead of main.py needing its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

from . import config

_counts: dict[str, int] = {}

# Live background graph tasks (main.py's _drive_graph) keyed by thread_id. Also replaces the old
# per-thread asyncio.Lock this module used to expose (get_lock, removed): with only one task ever
# driving a given thread's graph, "is there already a registered task" is itself the serialization
# -- a second concurrent request just becomes a subscriber (see subscribe() below) instead of
# racing a second astream_events call, so a separate lock has nothing left to guard.
_tasks: dict[str, "asyncio.Task[None]"] = {}

# Subscribers (one bounded asyncio.Queue per attached HTTP request) fed by _tasks' published
# events. A dedicated sentinel object (not None -- a real event could plausibly be falsy-ish)
# signals "this thread's background task has ended, stop yielding."
DONE: object = object()

_subscribers: dict[str, list["asyncio.Queue[Any]"]] = {}

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


def register_task(session_id: str, task: "asyncio.Task[None]") -> None:
    """Records the detached background task actually driving this thread's graph execution
    (main.py's _drive_graph). One caller: the HTTP request that finds no existing task for this
    thread_id and spawns one -- every other concurrently-attaching request is a reattach that only
    subscribes (see module docstring)."""
    _tasks[session_id.lower()] = task


def get_task(session_id: str) -> "asyncio.Task[None] | None":
    """The live task for this thread, or None if none is running (never started, or already
    finished/cancelled -- callers that care about 'still running' should also check `.done()`)."""
    return _tasks.get(session_id.lower())


def pop_task(session_id: str) -> "asyncio.Task[None] | None":
    """Removes and returns this thread's task entry -- _drive_graph's own `finally` calls this on
    every exit path (normal finish, crash, cancellation) so a finished task never lingers as if
    still live."""
    return _tasks.pop(session_id.lower(), None)


def cancel_run(session_id: str) -> bool:
    """Cancels the live background graph task for this thread, if any -- wired into
    sessions_api.py's terminate_session/delete_session_full ("Stop container") so Stop actually
    stops an in-flight LLM-only node too, not just the sandbox it may or may not be touching yet.
    Returns whether there was actually a live task to cancel (false is a normal outcome: the
    session may be paused at a gate, already finished, or never started)."""
    task = _tasks.get(session_id.lower())
    if task is None or task.done():
        return False
    task.cancel()
    return True


def subscribe(session_id: str) -> "asyncio.Queue[Any]":
    """Attaches a new subscriber queue for this thread's published events (one per HTTP request
    generator, main.py's run()). Must be called BEFORE any asyncio.create_task(_drive_graph(...))
    for a brand-new thread_id, in the same synchronous stretch (no `await` between the two) -- the
    task can start publishing (its first event) the moment it's created, and a subscriber added
    afterwards would miss it."""
    queue: "asyncio.Queue[Any]" = asyncio.Queue(maxsize=config.RUN_SUBSCRIBER_QUEUE_MAXSIZE)
    _subscribers.setdefault(session_id.lower(), []).append(queue)
    return queue


def unsubscribe(session_id: str, queue: "asyncio.Queue[Any]") -> None:
    """Detaches one subscriber -- called from the request generator's `finally`, so an HTTP
    disconnect only ever removes this one queue, never touches the background task or any other
    attached tab."""
    key = session_id.lower()
    subscribers = _subscribers.get(key)
    if subscribers is None:
        return
    with contextlib.suppress(ValueError):
        subscribers.remove(queue)
    if not subscribers:
        _subscribers.pop(key, None)


def publish(session_id: str, item: Any) -> None:
    """Fans one event (or DONE) out to every subscriber currently attached to this thread. A full
    queue means its reader has fallen behind (backgrounded/stalled tab, not necessarily dead) --
    drop the OLDEST queued item to make room rather than blocking the publisher or dropping the
    subscriber outright: dropping the subscriber would leave its generator awaiting a queue nobody
    will ever feed or close again, i.e. the exact kind of hang this whole mechanism exists to
    avoid, just moved to the read side. A genuinely dead subscriber is cleaned up the normal way,
    by its own HTTP disconnect cancelling its `queue.get()` await (see unsubscribe above)."""
    for queue in _subscribers.get(session_id.lower(), []):
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)


async def cancel_all_tasks() -> None:
    """Called once, from main.py's _lifespan shutdown, BEFORE closing the checkpointer connection.
    After this fix, background graph tasks are orphaned from any HTTP request, so uvicorn's normal
    graceful-shutdown drain (which only waits for in-flight requests) no longer covers them -- left
    unhandled, a task could still be writing a checkpoint the instant the SQLite connection
    underneath it closes. return_exceptions=True: a task's own CancelledError (or any other
    exception surfacing at cancellation) must not block the other tasks from being awaited too."""
    tasks = [task for task in _tasks.values() if not task.done()]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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

    # Task registry: register/get/pop/cancel, case-insensitive same as everything else here.
    async def _task_registry() -> None:
        async def _never_ending() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_never_ending())
        register_task("TASK-1", task)
        assert get_task("task-1") is task
        assert cancel_run("task-1") is True, "must cancel the registered task"
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        # cancel_run alone does not pop -- _drive_graph's own finally does that in real usage
        # (see its docstring); the entry sits cancelled-but-registered until something pops it.
        assert get_task("task-1") is task
        # cancel_run on an already-cancelled/never-registered thread is a normal no-op, not an error.
        assert cancel_run("task-1") is False
        assert cancel_run("never-registered") is False
        assert pop_task("task-1") is task
        assert get_task("task-1") is None
        register_task("task-2", task)
        assert pop_task("TASK-2") is task
        assert get_task("task-2") is None

    asyncio.run(_task_registry())

    # Subscriber fan-out: two subscribers on the same thread both see a published event; a
    # subscriber on a DIFFERENT thread sees nothing.
    async def _fan_out() -> None:
        q1 = subscribe("fanout-check")
        q2 = subscribe("fanout-check")
        q_other = subscribe("other-thread")
        publish("fanout-check", "event-1")
        assert q1.get_nowait() == "event-1"
        assert q2.get_nowait() == "event-1"
        assert q_other.empty()
        unsubscribe("fanout-check", q1)
        publish("fanout-check", "event-2")
        assert q1.empty(), "unsubscribed queue must not receive further events"
        assert q2.get_nowait() == "event-2"
        unsubscribe("fanout-check", q2)
        unsubscribe("other-thread", q_other)

    asyncio.run(_fan_out())

    # Backpressure: a full queue drops the OLDEST item to make room for the newest, rather than
    # blocking publish() or leaving the subscriber orphaned (see publish's own docstring).
    async def _backpressure() -> None:
        real_maxsize = config.RUN_SUBSCRIBER_QUEUE_MAXSIZE
        config.RUN_SUBSCRIBER_QUEUE_MAXSIZE = 2
        try:
            queue = subscribe("backpressure-check")
            publish("backpressure-check", "a")
            publish("backpressure-check", "b")
            publish("backpressure-check", "c")  # queue was full at [a, b] -- must drop "a"
            assert queue.get_nowait() == "b"
            assert queue.get_nowait() == "c"
            assert queue.empty()
            unsubscribe("backpressure-check", queue)
        finally:
            config.RUN_SUBSCRIBER_QUEUE_MAXSIZE = real_maxsize

    asyncio.run(_backpressure())

    # DONE sentinel reaches every subscriber, same as any other published item.
    async def _done_propagates() -> None:
        queue = subscribe("done-check")
        publish("done-check", DONE)
        assert queue.get_nowait() is DONE
        unsubscribe("done-check", queue)

    asyncio.run(_done_propagates())

    # cancel_all_tasks: cancels every live task and waits for them, swallowing their
    # CancelledError, without raising even when one is already finished.
    async def _cancel_all() -> None:
        async def _never_ending() -> None:
            await asyncio.sleep(10)

        finished = asyncio.create_task(_never_ending())
        finished.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await finished
        register_task("cancel-all-1", asyncio.create_task(_never_ending()))
        register_task("cancel-all-2", finished)  # already done -- must be skipped, not re-awaited
        await cancel_all_tasks()
        assert get_task("cancel-all-1").cancelled()

    asyncio.run(_cancel_all())

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
