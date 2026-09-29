"""Durable store for on-demand "Generate Code Health Report" jobs (SQL Server, `agent/db/
migrations/0018_create_health_reports.sql`).

One row per job, mutated in place through queued -> running -> completed|failed. Deliberately a
separate table from `dbo.sessions` (session_store.py): a report job has no project, no work
branch, no PR, and its sandbox is torn down the instant the scan finishes -- session_store.py's
schema (NOT NULL project_id/work_branch, a status vocabulary with no "queued"/"running") doesn't
fit a project-less, gate-less scan job.

Self-check runs against a real DB (local or Azure, whichever `db.py` resolves to): `cd agent &&
uv run python -m src.health_report_store`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aioodbc

from . import db

logger = logging.getLogger(__name__)

_COLUMNS = (
    "job_id", "owner", "repo", "branch", "user_login", "status",
    "report_json", "error_message", "created_at", "completed_at",
)

# Statuses a fresh process treats as "predates me, and I have no memory of provisioning it" --
# see health_report.reap_orphaned_jobs, the only other reader of this exact set.
ACTIVE_STATUSES = ("queued", "running")

_pool: aioodbc.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> aioodbc.Pool:
    """Same double-checked-lock pattern as session_store.py's own _get_pool -- see that
    function's docstring for why the lock is needed at all."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await aioodbc.create_pool(
                    autocommit=True, **(await asyncio.to_thread(db.connection_kwargs))
                )
    return _pool


def _row_to_dict(row: Any) -> dict[str, Any]:
    result = dict(zip(_COLUMNS, row))
    if result.get("job_id"):
        result["job_id"] = str(result["job_id"]).lower()
    if result.get("report_json"):
        result["report_json"] = json.loads(result["report_json"])
    return result


async def create_job(job_id: str, *, owner: str, repo: str, branch: str, user_login: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dbo.health_reports (job_id, owner, repo, branch, user_login, status)
            VALUES (?, ?, ?, ?, ?, 'queued')
            """,
            job_id, owner, repo, branch, user_login,
        )


async def has_active_job(owner: str, repo: str) -> bool:
    """health_report_api.py's 409 guard: at most one queued/running report job per (owner, repo)
    at a time -- adapted from sessions_api.py's _reject_if_another_ticket_open, same reasoning
    (an unbounded number of tabs/clicks would otherwise each provision a real clone-and-bootstrap
    container)."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP (1) job_id FROM dbo.health_reports "
            "WHERE owner = ? AND repo = ? AND status IN ('queued', 'running')",
            owner, repo,
        )
        return await cur.fetchone() is not None


async def mark_running(job_id: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE dbo.health_reports SET status = 'running' WHERE job_id = ?", job_id)


async def mark_completed(job_id: str, report: dict[str, Any]) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE dbo.health_reports
            SET status = 'completed', report_json = ?, completed_at = SYSUTCDATETIME()
            WHERE job_id = ?
            """,
            json.dumps(report), job_id,
        )


async def mark_failed(job_id: str, error_message: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE dbo.health_reports
            SET status = 'failed', error_message = ?, completed_at = SYSUTCDATETIME()
            WHERE job_id = ?
            """,
            error_message[:1000], job_id,
        )


async def get_job(job_id: str) -> dict[str, Any] | None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM dbo.health_reports WHERE job_id = ?", job_id)
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_active_jobs() -> list[dict[str, Any]]:
    """Every job still `queued`/`running` -- health_report.reap_orphaned_jobs' only caller,
    reading this once at process startup. A fresh process cannot have legitimately started any
    work of its own yet, so every row this returns predates it and is, by definition, orphaned
    (its sandbox provider's in-memory registry was wiped by the same restart)."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dbo.health_reports WHERE status IN ('queued', 'running')"
        )
        rows = await cur.fetchall()
        return [_row_to_dict(row) for row in rows]


async def _demo() -> None:
    import uuid

    job_id = str(uuid.uuid4())
    await create_job(job_id, owner="octocat", repo="hello-world", branch="main", user_login="octocat")
    assert await has_active_job("octocat", "hello-world")
    await mark_running(job_id)
    row = await get_job(job_id)
    assert row is not None and row["status"] == "running"
    await mark_completed(job_id, {"schema_version": 1, "summary": {"health_score": 87}})
    row = await get_job(job_id)
    assert row is not None and row["status"] == "completed" and row["report_json"]["summary"]["health_score"] == 87
    assert not await has_active_job("octocat", "hello-world")
    print("health_report_store self-check OK")


if __name__ == "__main__":
    asyncio.run(_demo())
