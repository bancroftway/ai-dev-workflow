"""Durable per-check verify history (SQL Server, `agent/db/migrations/0022_create_verify_check_results.sql`).

One row per CheckResult (gates/checks.py) per verify attempt. The verify nodes call
`append_results` once per attempt; sessions_api.py reads it back through `list_attempts` (a
session's verify history) and `check_stats` (a repo's per-check fail rates and attempts-to-pass).

Same shape as run_event_store.py: plain async functions over session_store's shared aioodbc pool,
fail-soft writes (a DB blip never raises into a verify node), chunked multi-row INSERTs.

Self-check runs against a real DB (local or Azure, whichever `db.py` resolves to): `cd agent &&
uv run python -m src.verify_check_store`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from . import config, session_store
from .gates.checks import resolve_code_gen_mode
from .text_truncate import truncate_middle

logger = logging.getLogger(__name__)

# Module-level name (not inlined) so _demo() can swap it to prove the fail-soft path.
_get_pool = session_store._get_pool

_COLUMNS = [
    "session_id", "run_id", "stage", "attempt", "timing", "code_gen_mode", "policy",
    "check_id", "status", "detail", "source", "stage_passed", "uncatalogued",
]
# 13 params per row; SQL Server caps one statement at 2100 params -> 161 rows max. 150 keeps
# headroom, same reasoning as run_event_store._MAX_ROWS_PER_INSERT.
_PARAMS_PER_ROW = len(_COLUMNS)
_MAX_ROWS_PER_INSERT = 150
# Schema widths from 0022 -- coupled to the DDL, not operator knobs.
_DETAIL_MAX_CHARS = 2000
_SOURCE_MAX_CHARS = 255
_CHECK_ID_MAX_CHARS = 128


def _clip_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    text = truncate_middle(
        str(detail), config.AIDW_VERIFY_CHECK_DETAIL_HEAD_CHARS, config.AIDW_VERIFY_CHECK_DETAIL_TAIL_CHARS,
    )
    return text[:_DETAIL_MAX_CHARS]  # oversized head+tail config must not fail the whole insert


async def append_results(
    session_id: str,
    run_id: str,
    stage: str,
    attempt: int,
    timing: str,
    code_gen_mode: str | None,
    policy: str,
    stage_passed: bool,
    checks: list[dict[str, Any]],
) -> int:
    """Inserts one row per `checks` entry (CheckResult.to_dict() shape: id/status/detail/source/
    [uncatalogued]). Returns how many rows were written. Fail-soft per chunk: a failure is logged
    and that chunk is dropped, never raised -- this is history, not a gate."""
    mode = resolve_code_gen_mode(code_gen_mode)
    rows = [
        [
            session_id, run_id, stage, attempt, timing, mode, policy,
            str(c["id"])[:_CHECK_ID_MAX_CHARS], c["status"], _clip_detail(c.get("detail")),
            (str(c["source"])[:_SOURCE_MAX_CHARS] if c.get("source") is not None else None),
            bool(stage_passed), bool(c.get("uncatalogued", False)),
        ]
        for c in checks
    ]
    written = 0
    for start in range(0, len(rows), _MAX_ROWS_PER_INSERT):
        chunk = rows[start : start + _MAX_ROWS_PER_INSERT]
        try:
            pool = await _get_pool()
            placeholders = ", ".join([f"({', '.join(['?'] * _PARAMS_PER_ROW)})"] * len(chunk))
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(
                    f"INSERT INTO dbo.verify_check_results ({', '.join(_COLUMNS)}) VALUES {placeholders}",
                    *[v for row in chunk for v in row],
                )
            written += len(chunk)
        except Exception:  # best-effort history; never abort the verify node over this
            logger.warning(
                "append_results failed for a %d-row chunk session_id=%s stage=%s attempt=%s -- continuing without it",
                len(chunk), session_id, stage, attempt, exc_info=True,
            )
    return written


async def list_attempts(session_id: str, stage: str | None = None) -> list[dict[str, Any]]:
    """One entry per (run_id, stage, attempt), oldest first (newest last), each with its checks.
    run_id is part of the key so a resumed run restarting at attempt 1 doesn't merge into the
    previous run's attempt 1."""
    sql = (
        "SELECT run_id, stage, attempt, timing, code_gen_mode, policy, stage_passed, created_at, "
        "check_id, status, detail, source, uncatalogued FROM dbo.verify_check_results WHERE session_id = ?"
    )
    params: list[Any] = [session_id]
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(sql + " ORDER BY id ASC", *params)
        rows = await cur.fetchall()
    attempts: dict[tuple[str, str, int], dict[str, Any]] = {}
    for run_id, stg, att, timing, mode, policy, passed, created_at, check_id, status, detail, source, unc in rows:
        entry = attempts.setdefault((run_id, stg, att), {
            "run_id": run_id, "stage": stg, "attempt": att, "timing": timing, "code_gen_mode": mode,
            "policy": policy, "stage_passed": bool(passed), "created_at": created_at, "checks": [],
        })
        entry["checks"].append({
            "id": check_id, "status": status, "detail": detail, "source": source, "uncatalogued": bool(unc),
        })
    return list(attempts.values())


async def check_stats(owner: str, repo: str, since: datetime | None = None, mode: str | None = None) -> dict[str, Any]:
    """Per-check fail rates and per-stage average attempts-to-pass for one repo (owner/repo via
    dbo.sessions). `runs` excludes skipped rows; `fail_rate` = fails / runs (0 when runs is 0).
    `since`: naive = UTC; aware is converted (created_at is naive-UTC DATETIME2)."""
    where = "s.owner = ? AND s.repo = ?"
    params: list[Any] = [owner, repo]
    if since is not None:
        where += " AND r.created_at >= ?"
        params.append(since.astimezone(UTC).replace(tzinfo=None) if since.tzinfo else since)
    if mode:
        where += " AND r.code_gen_mode = ?"
        params.append(mode)
    source = f"dbo.verify_check_results r JOIN dbo.sessions s ON s.session_id = r.session_id WHERE {where}"
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT r.check_id, r.stage,
                   SUM(CASE WHEN r.status <> 'skipped' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.status = 'failed' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN r.status = 'infra' THEN 1 ELSE 0 END),
                   MAX(CASE WHEN r.status = 'failed' THEN r.created_at END)
            FROM {source}
            GROUP BY r.check_id, r.stage
            """,
            *params,
        )
        check_rows = await cur.fetchall()
        # attempts-to-pass for one (session, stage) = the first attempt whose verdict passed;
        # AVG skips sessions that never passed (NULL), `sessions` still counts them.
        await cur.execute(
            f"""
            SELECT stage, AVG(CAST(pass_attempt AS FLOAT)), COUNT(*)
            FROM (
                SELECT r.session_id, r.stage, MIN(CASE WHEN r.stage_passed = 1 THEN r.attempt END) AS pass_attempt
                FROM {source}
                GROUP BY r.session_id, r.stage
            ) per_session
            GROUP BY stage
            """,
            *params,
        )
        stage_rows = await cur.fetchall()
    checks = [
        {
            "check_id": check_id, "stage": stage, "runs": runs, "fails": fails, "infra": infra,
            "fail_rate": (fails / runs) if runs else 0.0, "last_failed": last_failed,
        }
        for check_id, stage, runs, fails, infra, last_failed in check_rows
    ]
    checks.sort(key=lambda c: (-c["fail_rate"], -c["fails"], c["check_id"]))
    stages = [
        {"stage": stage, "avg_attempts_to_pass": avg, "sessions": sessions}
        for stage, avg, sessions in sorted(stage_rows, key=lambda r: r[0])
    ]
    return {"checks": checks, "stages": stages}


async def delete_by_session(session_id: str) -> None:
    """Must run before session_store.delete_session: 0022's session_id FK has no ON DELETE CASCADE."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.verify_check_results WHERE session_id = ?", session_id)


async def _demo() -> None:
    """Self-check against a real DB: `cd agent && uv run python -m src.verify_check_store`."""
    global _get_pool  # reassigned below (fail-soft check)
    project_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    owner, repo = "octocat", f"demo-repo-verify-check-selfcheck-{uuid.uuid4().hex[:6]}"
    run_id = uuid.uuid4().hex[:8]
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO dbo.projects (project_id, name, created_by) VALUES (?, ?, ?)",
            project_id, "verify-check-store-selfcheck-project", "octocat",
        )
        await cur.execute(
            """
            INSERT INTO dbo.sessions
                (session_id, owner, repo, user_login, title, source_branch, work_branch, project_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
            """,
            session_id, owner, repo, "octocat", "t", "main", f"ai-dev-workflow/{session_id}", project_id,
        )
    try:
        long_detail = "HEAD" + "x" * 10_000 + "TAIL"
        attempt1 = [
            {"id": "spec_has_ac", "status": "failed", "detail": long_detail, "source": "spec_verify › graph.verify"},
            {"id": "spec_diagram_renders", "status": "passed", "detail": None, "source": "spec_verify › x"},
            {"id": "stray", "status": "infra", "detail": "boom", "source": "s", "uncatalogued": True},
        ]
        attempt2 = [
            {"id": "spec_has_ac", "status": "passed", "detail": None, "source": "s"},
            {"id": "spec_diagram_renders", "status": "skipped", "detail": None, "source": "s"},
        ]
        n = await append_results(session_id, run_id, "specification", 1, "before_review", "draft_verify", "blocking", False, attempt1)
        assert n == 3, n
        n = await append_results(session_id, run_id, "specification", 2, "before_review", "draft_verify", "blocking", True, attempt2)
        assert n == 2, n
        # None mode resolves to the strictest mode instead of tripping the CHECK constraint.
        await append_results(session_id, run_id, "plan", 1, "before_review", None, "advisory", True,
                             [{"id": "plan_ok", "status": "passed", "detail": None, "source": "s"}])

        attempts = await list_attempts(session_id)
        assert [(a["stage"], a["attempt"]) for a in attempts] == [("specification", 1), ("specification", 2), ("plan", 1)], attempts
        assert attempts[2]["code_gen_mode"] == "mission_critical", attempts[2]
        first = attempts[0]
        assert first["stage_passed"] is False and len(first["checks"]) == 3, first
        stored = first["checks"][0]["detail"]
        assert stored.startswith("HEAD") and stored.endswith("TAIL") and "chars omitted" in stored, stored[:50]
        assert len(stored) <= _DETAIL_MAX_CHARS, len(stored)
        assert first["checks"][2]["uncatalogued"] is True and first["checks"][0]["uncatalogued"] is False
        assert [a["attempt"] for a in await list_attempts(session_id, stage="plan")] == [1]

        stats = await check_stats(owner, repo)
        by_id = {c["check_id"]: c for c in stats["checks"]}
        assert by_id["spec_has_ac"]["runs"] == 2 and by_id["spec_has_ac"]["fails"] == 1, by_id
        assert by_id["spec_has_ac"]["fail_rate"] == 0.5 and by_id["spec_has_ac"]["last_failed"] is not None
        assert by_id["spec_diagram_renders"]["runs"] == 1, "skipped rows must not count as runs"
        assert by_id["stray"]["infra"] == 1 and by_id["stray"]["fail_rate"] == 0.0
        assert stats["checks"][0]["check_id"] == "spec_has_ac", "ranked by fail rate"
        stages = {s["stage"]: s for s in stats["stages"]}
        assert stages["specification"] == {"stage": "specification", "avg_attempts_to_pass": 2.0, "sessions": 1}, stages
        assert stages["plan"]["avg_attempts_to_pass"] == 1.0, stages
        assert (await check_stats(owner, repo, mode="yolo")) == {"checks": [], "stages": []}
        assert (await check_stats(owner, repo, since=datetime(2999, 1, 1, tzinfo=UTC))) == {"checks": [], "stages": []}
        assert len((await check_stats(owner, repo, mode="draft_verify"))["checks"]) == 3

        # Chunking: 400 rows > one 150-row chunk, all must land.
        many = [{"id": f"c{i}", "status": "passed", "detail": None, "source": "s"} for i in range(400)]
        assert await append_results(session_id, run_id, "build", 1, "after_submit", "yolo", "off", True, many) == 400
        assert len((await list_attempts(session_id, stage="build"))[0]["checks"]) == 400

        # Fail-soft: a DB outage returns 0, never raises.
        real_get_pool = _get_pool

        async def _broken_pool():
            raise RuntimeError("simulated DB outage")

        _get_pool = _broken_pool
        try:
            assert await append_results(session_id, run_id, "x", 1, "before_review", "yolo", "off", True, attempt2) == 0
        finally:
            _get_pool = real_get_pool

        await delete_by_session(session_id)
        assert await list_attempts(session_id) == []
        print("verify_check_store self-check: ok")
    finally:
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM dbo.verify_check_results WHERE session_id = ?", session_id)
            await cur.execute("DELETE FROM dbo.sessions WHERE session_id = ?", session_id)
            await cur.execute("DELETE FROM dbo.projects WHERE project_id = ?", project_id)


async def _demo_and_close() -> None:
    await _demo()
    pool = await _get_pool()
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.verify_check_store
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo_and_close())
