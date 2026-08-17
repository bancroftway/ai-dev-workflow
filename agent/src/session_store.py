"""Single source of truth for session metadata (SQL Server, `agent/db/migrations/0001_create_sessions.sql`)
-- replaces the git-committed `.ai-dev-workflow/sessions.json` (session_index.py) and the durable
half of `sandbox/registry.py`'s `_meta`.

One row per session, mutated in place through its lifecycle (in_progress -> completed|failed|
rejected), never one row per attempt -- `session_id` is the primary key and IS the LangGraph
thread_id / sandbox session_id, so there is nothing to upsert-by-thread the way the old JSON file
had to.

Self-check runs against a real DB (local or Azure, whichever `db.py` resolves to): `cd agent &&
uv run python -m src.session_store`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aioodbc

from . import db

logger = logging.getLogger(__name__)

_DEFAULT_LIST_LIMIT = 20

_pool: aioodbc.Pool | None = None


async def _get_pool() -> aioodbc.Pool:
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(autocommit=True, **db.connection_kwargs())
    return _pool


def _build_failure(payload: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """Raw run_failure payload (shape varies per escalate_node) -> (stage, type, message) --
    same normalization session_index.py's _build_failure did, kept as three columns instead of
    a nested dict."""
    raw_message = payload.get("feedback") or payload.get("report") or ""
    return payload.get("stage"), payload.get("type"), str(raw_message).strip()[:500]


def _row_to_dict(columns: list[str], row: Any) -> dict[str, Any]:
    """SQL Server returns UNIQUEIDENTIFIER as an uppercase string -- normalize to lowercase so it
    matches the lowercase uuid4() every caller (frontend included) mints and compares against."""
    result = dict(zip(columns, row))
    if result.get("session_id"):
        result["session_id"] = str(result["session_id"]).lower()
    return result


async def create_session(
    session_id: str,
    *,
    owner: str,
    repo: str,
    user_login: str,
    source_branch: str,
    work_branch: str,
    title: str,
) -> None:
    """Idempotent: called from sessions_api.provision_session before the sandbox boots. A
    reattach (same session_id provisioned again) is a no-op here -- touch_run is what refreshes
    a session's live state on each scaffold_node round."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM dbo.sessions WHERE session_id = ?)
            INSERT INTO dbo.sessions
                (session_id, owner, repo, user_login, title, source_branch, work_branch, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress')
            """,
            session_id,
            session_id,
            owner,
            repo,
            user_login,
            title or "(untitled session)",
            source_branch,
            work_branch,
        )


async def touch_run(session_id: str, *, run_id: str, title: str | None) -> None:
    """Replaces session_index.start_session -- called from scaffold_node every round (first
    start AND every resume). Resets the row back to a live in_progress state and clears every
    terminal-outcome field from a PRIOR attempt: without this, a session that failed, got
    resumed, and then succeeded would still show its earlier failure reason and a stale pr_url.

    run_id/title only refresh together when title is non-empty and differs from the current
    title (same heuristic session_index.py used) -- a clarification round that repeats the same
    title, or has none yet, keeps the original run's identity instead of minting a new run_id
    per round."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT title, run_id FROM dbo.sessions WHERE session_id = ?", session_id)
        row = await cur.fetchone()
        current_title, current_run_id = (row[0], row[1]) if row else (None, None)
        # current_run_id is None on the very first round (create_session never sets it) -- that
        # must always refresh, even though the title it's paired with is create_session's own
        # initial title and so wouldn't otherwise look "different."
        refresh_identity = current_run_id is None or (bool(title) and title != current_title)
        new_run_id = run_id if refresh_identity else current_run_id
        new_title = title if refresh_identity else current_title

        await cur.execute(
            """
            UPDATE dbo.sessions
            SET status = 'in_progress',
                ended_at = NULL,
                failure_stage = NULL, failure_type = NULL, failure_message = NULL,
                merge_ready = NULL, pr_title = NULL, pr_url = NULL,
                run_id = ?, title = ?,
                updated_at = SYSUTCDATETIME()
            WHERE session_id = ?
            """,
            new_run_id,
            new_title,
            session_id,
        )


async def update_current_stage(session_id: str, stage_key: str) -> None:
    """Called from make_gate_node's gate_node closure right after a stage's approval succeeds --
    the one choke point every stage's gate already passes through. Drives the session-list UI's
    progress indicator."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.sessions SET current_stage = ?, updated_at = SYSUTCDATETIME() WHERE session_id = ?",
            stage_key,
            session_id,
        )


async def close_session(
    session_id: str,
    *,
    run_id: str | None,
    status: str,
    failure: dict[str, Any] | None = None,
    merge_ready: bool | None = None,
    pr_title: str | None = None,
    pr_url: str | None = None,
) -> None:
    """Replaces session_index.end_session / the DB half of git_ops.record_run_failure. Always
    sets ended_at -- this is a terminal close for the current attempt."""
    failure_stage, failure_type, failure_message = _build_failure(failure) if failure else (None, None, None)
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE dbo.sessions
            SET status = ?, ended_at = SYSUTCDATETIME(),
                run_id = COALESCE(?, run_id),
                failure_stage = ?, failure_type = ?, failure_message = ?,
                merge_ready = ?, pr_title = ?, pr_url = ?,
                updated_at = SYSUTCDATETIME()
            WHERE session_id = ?
            """,
            status,
            run_id,
            failure_stage,
            failure_type,
            failure_message,
            merge_ready,
            pr_title,
            pr_url,
            session_id,
        )


_COLUMNS = [
    "session_id", "owner", "repo", "user_login", "title", "source_branch", "work_branch",
    "run_id", "current_stage", "status", "started_at", "ended_at", "merge_ready",
    "pr_title", "pr_url", "failure_stage", "failure_type", "failure_message", "updated_at",
]


async def get_session(session_id: str) -> dict[str, Any] | None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM dbo.sessions WHERE session_id = ?", session_id)
        row = await cur.fetchone()
        return _row_to_dict(_COLUMNS, row) if row else None


async def list_sessions(
    owner: str, repo: str, source_branch: str | None = None, limit: int = _DEFAULT_LIST_LIMIT
) -> list[dict[str, Any]]:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        if source_branch:
            await cur.execute(
                f"SELECT TOP (?) {', '.join(_COLUMNS)} FROM dbo.sessions "
                "WHERE owner = ? AND repo = ? AND source_branch = ? ORDER BY started_at DESC",
                limit, owner, repo, source_branch,
            )
        else:
            await cur.execute(
                f"SELECT TOP (?) {', '.join(_COLUMNS)} FROM dbo.sessions "
                "WHERE owner = ? AND repo = ? ORDER BY started_at DESC",
                limit, owner, repo,
            )
        rows = await cur.fetchall()
        return [_row_to_dict(_COLUMNS, row) for row in rows]


async def _demo() -> None:
    """Self-check against a real DB: `cd agent && uv run python -m src.session_store`."""
    session_id = str(uuid.uuid4())
    owner, repo = "octocat", "demo-repo-session-store-selfcheck"
    try:
        await create_session(
            session_id,
            owner=owner,
            repo=repo,
            user_login="octocat",
            source_branch="main",
            work_branch=f"ai-dev-workflow/{session_id}",
            title="Initial request",
        )
        row = await get_session(session_id)
        assert row is not None and row["status"] == "in_progress" and row["title"] == "Initial request", row

        await touch_run(session_id, run_id="r1", title="Initial request")
        row = await get_session(session_id)
        assert row["run_id"] == "r1", row

        # Same title on a later round keeps the original run_id (clarification-round heuristic).
        await touch_run(session_id, run_id="r2", title="Initial request")
        row = await get_session(session_id)
        assert row["run_id"] == "r1", row

        # A genuinely different title refreshes run_id.
        await touch_run(session_id, run_id="r3", title="Initial request, revised")
        row = await get_session(session_id)
        assert row["run_id"] == "r3" and row["title"] == "Initial request, revised", row

        await update_current_stage(session_id, "tech-stack")
        row = await get_session(session_id)
        assert row["current_stage"] == "tech-stack", row

        await close_session(
            session_id, run_id="r3", status="failed",
            failure={"stage": "exit", "type": "gates_not_passed", "feedback": "missing screenshots"},
        )
        row = await get_session(session_id)
        assert row["status"] == "failed" and row["failure_message"] == "missing screenshots", row
        assert row["ended_at"] is not None, row

        # Resume: touch_run must clear the stale failure/pr fields from the failed attempt above.
        await touch_run(session_id, run_id="r4", title="Initial request, revised, again")
        row = await get_session(session_id)
        assert row["status"] == "in_progress", row
        assert row["ended_at"] is None, row
        assert row["failure_stage"] is None and row["failure_message"] is None, row

        await close_session(
            session_id, run_id="r4", status="completed",
            merge_ready=True, pr_title="Add the thing", pr_url="https://github.com/o/r/pull/1",
        )
        row = await get_session(session_id)
        assert row["status"] == "completed" and row["pr_url"] == "https://github.com/o/r/pull/1", row

        sessions = await list_sessions(owner, repo)
        assert any(s["session_id"] == session_id for s in sessions), sessions

        print("session_store self-check: ok")
    finally:
        pool = await _get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM dbo.sessions WHERE session_id = ?", session_id)


async def _demo_and_close() -> None:
    await _demo()
    pool = await _get_pool()
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.session_store
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo_and_close())
