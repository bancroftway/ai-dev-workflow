"""Durable LangGraph checkpointing (2026-08-31).

Swaps the compiled graph's boot-time InMemorySaver for a first-party AsyncSqliteSaver so open
human gates and in-flight thread state survive agent restarts -- the in-memory saver cost four
spec redrafts in one working session, because every restart abandoned the paused interrupt and
forced the reattach run to redraft the gated stage.

One SQLite file serves the whole agent (all repos, all sessions), rows keyed by thread_id
(== dbo.sessions.session_id); repo attribution is a join through dbo.sessions. This is
deliberately NOT per-repo and NOT inside a target repo's .ai-dev-workflow/ folder: the saver
attaches once to the one compiled graph, the sandbox clone offers no direct file I/O (docker-exec
only), and a binary full-history DB has no business on a pushed work branch. The repo-resident
half of persistence (state.json + numbered artifacts, workflow_persistence.py) is unchanged and
remains the recovery source if this file is ever lost.

Single-process constraint: valid only while uvicorn runs ONE worker -- true today, and the
sandbox registry / push-token map already demand it. Multi-process scale-out means the Postgres
saver as part of a much larger change.

Deploy note: in the containerized/Azure deployment AIDW_CHECKPOINT_DB must point at a mounted
volume, or checkpoints die with the container.

Retention: checkpoints accrue per super-step per thread with no TTL. delete_thread_checkpoints
below covers session deletion; pruning threads whose dbo.sessions row is terminal is a backlog
item, not built yet.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).parents[1] / "data" / "checkpoints.sqlite"

_saver: Any | None = None
_conn: Any | None = None


def db_path() -> Path:
    return Path(os.environ.get("AIDW_CHECKPOINT_DB") or _DEFAULT_DB_PATH)


async def attach_sqlite_checkpointer(graph: Any) -> Any | None:
    """Attach a durable AsyncSqliteSaver to the compiled graph, replacing the InMemorySaver it
    booted with.

    Called from an async context at process startup (FastAPI lifespan / run_headless) because
    the saver needs an open aiosqlite connection plus one-time setup() DDL. The attribute swap
    is safe: the installed Pregel resolves `self.checkpointer` at run time on every invoke
    (verified against langgraph 1.2.10), and startup completes before the first request.

    Idempotent -- a second call returns the already-attached saver rather than leaking another
    connection. Fail-soft: any failure logs loudly and leaves the InMemorySaver in place; a boot
    must never be blocked by checkpoint durability (behavior then degrades exactly to the
    pre-durability world, with workflow_persistence hydration as the cross-restart fallback).
    """
    global _saver, _conn
    if _saver is not None:
        graph.checkpointer = _saver
        return _saver
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = await aiosqlite.connect(path)
        saver = AsyncSqliteSaver(_conn)
        await saver.setup()
        graph.checkpointer = saver
        _saver = saver
        logger.info("durable checkpointer attached: AsyncSqliteSaver at %s", path)
        return saver
    except Exception:  # noqa: BLE001 - never block boot on checkpoint durability
        logger.warning(
            "durable checkpointer attach FAILED -- continuing on the in-memory saver "
            "(gates will not survive a restart)",
            exc_info=True,
        )
        return None


async def close_checkpointer() -> None:
    """Shutdown counterpart for the FastAPI lifespan -- closes the aiosqlite connection."""
    global _saver, _conn
    if _conn is not None:
        try:
            await _conn.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.warning("closing checkpoint connection failed", exc_info=True)
    _saver = None
    _conn = None


async def delete_thread_checkpoints(thread_id: str) -> None:
    """Best-effort removal of one thread's checkpoint history -- called from the session delete
    path so the file tracks the session list instead of growing forever. hasattr-guarded: not
    every saver version ships adelete_thread."""
    if _saver is None or not hasattr(_saver, "adelete_thread"):
        return
    try:
        await _saver.adelete_thread(thread_id)
        logger.info("checkpoints deleted for thread_id=%s", thread_id)
    except Exception:  # noqa: BLE001 - teardown steps are fail-soft, like the rest of delete
        logger.warning("deleting checkpoints failed for thread_id=%s", thread_id, exc_info=True)
