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

import asyncio
import logging
import uuid
from typing import Any

import aioodbc

from . import db

logger = logging.getLogger(__name__)

_DEFAULT_LIST_LIMIT = 20

_pool: aioodbc.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> aioodbc.Pool:
    """The `is None` check below is not atomic with the assignment -- without the lock, several
    early concurrent callers (observed live 2026-09-01: a burst of GET /sessions/{id} polls right
    after a restart) each see None, each call create_pool(), and only the last assignment
    survives -- the other pool(s), maxsize connections apiece, are instantly unreferenced and
    surface as "Unclosed connection" asyncio errors when GC'd. Standard double-checked lock:
    still no lock taken on the fast path once _pool is set."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                # to_thread: db.connection_kwargs() does synchronous HTTP (an AAD token fetch) in
                # Azure mode -- run it off the event loop rather than blocking every other
                # coroutine on it.
                _pool = await aioodbc.create_pool(
                    autocommit=True, **(await asyncio.to_thread(db.connection_kwargs))
                )
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
    if result.get("project_id"):
        result["project_id"] = str(result["project_id"]).lower()
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
    project_id: str,
    provider: str | None = None,
) -> None:
    """Idempotent: called from sessions_api.provision_session before the sandbox boots. A
    reattach (same session_id provisioned again) is a no-op here -- touch_run is what refreshes
    a session's live state on each scaffold_node round.

    project_id is required, not optional -- every session/ticket belongs to exactly one project
    from this migration forward (Ruling 1, docs/superpowers/plans/part-3-tickets-tasks.md); a
    caller that forgets to resolve one gets a TypeError, not a silent NULL.

    provider (Phase E audit I-3, 0008_add_sessions_provider.sql): the "copilot"/"claude" this
    session's FIRST real provision actually used -- written once, here, and never updated again
    (mirrors GraphState.provider's own "pinned per-thread, never re-resolved" contract one layer
    up). Optional/None only because the IF NOT EXISTS guard below makes this whole INSERT a no-op
    on a reattach -- a caller re-provisioning an existing row is not, in practice, expected to omit
    it, but nothing here enforces that; provision_session always resolves and passes a real value
    for a genuinely new session, which is the only case this INSERT ever actually fires for."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            IF NOT EXISTS (SELECT 1 FROM dbo.sessions WHERE session_id = ?)
            INSERT INTO dbo.sessions
                (session_id, owner, repo, user_login, title, source_branch, work_branch, project_id, provider, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
            """,
            session_id,
            session_id,
            owner,
            repo,
            user_login,
            title or "(untitled session)",
            source_branch,
            work_branch,
            project_id,
            provider,
        )


async def touch_run(session_id: str, *, run_id: str, title: str | None) -> None:
    """Replaces session_index.start_session -- called from scaffold_node every round (first
    start AND every resume). Resets the row back to a live in_progress state and clears every
    terminal-outcome field from a PRIOR attempt: without this, a session that failed, got
    resumed, and then succeeded would still show its earlier failure reason and a stale pr_url.

    run_id/title only refresh together when title is non-empty and differs from the current
    title (same heuristic session_index.py used) -- a clarification round that repeats the same
    title, or has none yet, keeps the original run's identity instead of minting a new run_id
    per round.

    Also clears awaiting_gate (Part 3 Task 1), same reasoning as the other stale-prior-attempt
    fields below: LangGraph's InMemorySaver checkpoint does not survive a process restart, so a
    session that was genuinely paused at a gate when the process died has no real interrupt left to
    resume into -- the next round re-enters via intake_node/draft_node like any other resume, not
    back into the old paused gate, so a stale True here would show the board a ⏸ that no longer
    means anything until that stage's gate is actually reached again."""
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
                awaiting_gate = 0,
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
    the one choke point every stage's gate already passes through (gate_node post-interrupt-resume,
    auto_approve_node, and make_draft_node's hydrate short-circuit all funnel through
    _run_post_approve_hook). Drives the session-list UI's progress indicator.

    Also unconditionally clears awaiting_gate (Part 3 Task 1): whichever of those three paths just
    approved this stage, it is no longer paused awaiting a human at its own gate. See
    set_awaiting_gate below and 0004_create_projects.sql's own comment for why this column exists
    at all -- current_stage on its own only ever advances post-approval, so it cannot distinguish
    "still drafting stage X" from "paused at stage X's gate" while either is in flight."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.sessions SET current_stage = ?, awaiting_gate = 0, updated_at = SYSUTCDATETIME() "
            "WHERE session_id = ?",
            stage_key,
            session_id,
        )


async def set_awaiting_gate(session_id: str, awaiting: bool) -> None:
    """Set to True immediately before make_gate_node's gate_node (graph.py) actually calls
    interrupt() and the graph run pauses -- the durable, cross-process signal that this session is
    sitting at a human gate right now. Needed because current_stage alone can't carry this (see
    update_current_stage above) and LangGraph's own record of "this thread is inside an
    interrupt()" lives only in the compiled graph's in-process InMemorySaver checkpointer -- never
    durable, never visible to another process, gone after a restart. The board's GET /sessions
    (Task 9) is a plain DB read and has no other way to see this.

    Cleared back to False unconditionally by update_current_stage's own UPDATE once the gate
    resolves, and by touch_run on the next resume -- so a process restart while paused (which loses
    the in-memory checkpoint) can't leave a stale True behind once that session is next touched."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.sessions SET awaiting_gate = ?, updated_at = SYSUTCDATETIME() WHERE session_id = ?",
            1 if awaiting else 0,
            session_id,
        )


async def set_session_provider(session_id: str, provider: str) -> None:
    """Backfill a pre-0008-migration row's NULL provider once a reprovision resolves one, so it
    stops falling back to a live re-resolve forever (Phase E audit I-3 review, Minor 3).

    create_session's own IF NOT EXISTS guard only ever WRITES provider on a session's first-ever
    provision -- a session created before migration 0008 added the column has provider=NULL
    permanently, since create_session never runs for it again. Without this, sessions_api.
    provision_session's stored-or-live resolution keeps resolving live on every single reprovision
    of that one legacy row, exactly the gap I-3 exists to close, just for however long that row
    keeps getting reused.

    `WHERE provider IS NULL` makes this safe to call unconditionally on every reprovision: a no-op
    once the row is stamped (idempotent), and it can never clobber an already-pinned value even if
    called with a different one by mistake -- provider, once set, stays exactly as write-once as
    create_session's own IF NOT EXISTS already makes it for a brand-new row."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.sessions SET provider = ?, updated_at = SYSUTCDATETIME() "
            "WHERE session_id = ? AND provider IS NULL",
            provider,
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
    sets ended_at -- this is a terminal close for the current attempt.

    Side effect (per-repo container cap, CI/CD plan Phase 5): every terminal transition also
    fire-and-forgets the session's container teardown. This is THE choke point all ended
    sessions pass through (exit_nodes' completed/failed paths, git_ops' push-failure close,
    deploy_drain), so hooking here frees the repo's one-container slot in seconds instead of
    the idle reaper's 30 minutes. Function-level import: the sandbox package transitively pulls
    chat_model, which a module-level import here would cycle. create_task, not await: ACI
    teardown shells `az container delete` (tens of seconds) and must not block the exit path;
    off-process callers (deploy_drain on a CI runner) no-op instantly inside the helper."""
    from .sandbox.factory import end_session_container

    asyncio.create_task(end_session_container(session_id))
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


async def delete_session(session_id: str) -> None:
    """Removes the row entirely -- distinct from close_session (which ends an attempt but keeps
    its history). Used only by the explicit "delete session" purge (sessions_api.delete_session_full),
    never by the ordinary run lifecycle."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.sessions WHERE session_id = ?", session_id)


_COLUMNS = [
    "session_id", "owner", "repo", "user_login", "title", "source_branch", "work_branch",
    "run_id", "current_stage", "status", "started_at", "ended_at", "merge_ready",
    "pr_title", "pr_url", "failure_stage", "failure_type", "failure_message", "updated_at",
    "project_id", "awaiting_gate", "provider",
]


async def get_session(session_id: str) -> dict[str, Any] | None:
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT {', '.join(_COLUMNS)} FROM dbo.sessions WHERE session_id = ?", session_id)
        row = await cur.fetchone()
        return _row_to_dict(_COLUMNS, row) if row else None


async def list_sessions(
    owner: str,
    repo: str,
    source_branch: str | None = None,
    project_id: str | None = None,
    limit: int = _DEFAULT_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """owner/repo stay required (the pre-existing /select history-panel query); source_branch and
    now project_id are both optional filters layered on top -- project_id backs the board's
    (Task 9) project-scoped listing via IX_sessions_project, e.g. GET /sessions?owner=&repo=&
    project_id=, since every project maps to exactly one owner/repo once it has one."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        conditions = ["owner = ?", "repo = ?"]
        params: list[Any] = [owner, repo]
        if source_branch:
            conditions.append("source_branch = ?")
            params.append(source_branch)
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        await cur.execute(
            f"SELECT TOP (?) {', '.join(_COLUMNS)} FROM dbo.sessions "
            f"WHERE {' AND '.join(conditions)} ORDER BY started_at DESC",
            limit, *params,
        )
        rows = await cur.fetchall()
        return [_row_to_dict(_COLUMNS, row) for row in rows]


async def sessions_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    """(session_id, owner, repo) for the given session ids -- the reverse direction of
    list_sessions (which requires owner/repo). Backs the per-repo container cap: callers join the
    registry's live thread_ids to repos (provision guard + GET /sessions/active)."""
    if not ids:
        return []
    cols = ["session_id", "owner", "repo"]
    placeholders = ", ".join("?" for _ in ids)
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(cols)} FROM dbo.sessions WHERE session_id IN ({placeholders})",
            *ids,
        )
        rows = await cur.fetchall()
        return [_row_to_dict(cols, row) for row in rows]


async def _demo() -> None:
    """Self-check against a real DB: `cd agent && uv run python -m src.session_store`."""
    session_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    owner, repo = "octocat", "demo-repo-session-store-selfcheck"
    pool = await _get_pool()
    # dbo.sessions.project_id is a real FK (0004_create_projects.sql) -- a throwaway dbo.projects
    # row satisfies it for this self-check. Raw SQL here (not project_store.create_project) keeps
    # this module's own self-check independent of a sibling module's API; cleaned up in `finally`
    # below alongside the session row itself.
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO dbo.projects (project_id, name, created_by) VALUES (?, ?, ?)",
            project_id, "session-store-selfcheck-project", "octocat",
        )
    try:
        await create_session(
            session_id,
            owner=owner,
            repo=repo,
            user_login="octocat",
            source_branch="main",
            work_branch=f"ai-dev-workflow/{session_id}",
            title="Initial request",
            project_id=project_id,
            provider="claude",
        )
        row = await get_session(session_id)
        assert row is not None and row["status"] == "in_progress" and row["title"] == "Initial request", row
        assert row["project_id"] == project_id, row
        assert not row["awaiting_gate"], row  # never set yet
        # Phase E audit I-3: written once at first provision, round-trips exactly -- this is the
        # value sessions_api.provision_session must prefer over a live get_provider() re-resolve
        # on every later reprovision of this same session.
        assert row["provider"] == "claude", row

        # The IF NOT EXISTS guard makes a second create_session call for the SAME session_id a
        # true no-op -- a reattach with a different (e.g. live-resolved) provider must NOT
        # overwrite the pinned value from the session's first real provision.
        await create_session(
            session_id, owner=owner, repo=repo, user_login="octocat", source_branch="main",
            work_branch=f"ai-dev-workflow/{session_id}", title="Initial request", project_id=project_id,
            provider="copilot",
        )
        row = await get_session(session_id)
        assert row["provider"] == "claude", (
            f"create_session's IF NOT EXISTS guard must leave the first-pinned provider alone, got {row['provider']!r}"
        )

        # set_session_provider (Phase E audit I-3 review, Minor 3): backfills a pre-0008-migration
        # row's NULL provider once something resolves one -- own session_id since `session_id`
        # above already has a real (non-NULL) provider from create_session.
        legacy_session_id = str(uuid.uuid4())
        await create_session(
            legacy_session_id, owner=owner, repo=repo, user_login="octocat", source_branch="main",
            work_branch=f"ai-dev-workflow/{legacy_session_id}", title="Legacy row", project_id=project_id,
            # provider omitted -- simulates a session created before migration 0008 (provider=NULL).
        )
        row = await get_session(legacy_session_id)
        assert row["provider"] is None, row  # premise check

        await set_session_provider(legacy_session_id, "copilot")
        row = await get_session(legacy_session_id)
        assert row["provider"] == "copilot", row

        # WHERE provider IS NULL guard: a second stamp with a DIFFERENT value must be a no-op, not
        # an overwrite -- once set (by this function or by create_session), provider stays exactly
        # as write-once as create_session's own IF NOT EXISTS already makes it for a brand-new row.
        await set_session_provider(legacy_session_id, "claude")
        row = await get_session(legacy_session_id)
        assert row["provider"] == "copilot", (
            f"set_session_provider must never overwrite an already-stamped value, got {row['provider']!r}"
        )
        await delete_session(legacy_session_id)

        # sessions_by_ids: reverse lookup for the per-repo container cap -- unknown ids drop out,
        # empty input short-circuits without touching the pool.
        assert await sessions_by_ids([]) == []
        by_ids = await sessions_by_ids([session_id, str(uuid.uuid4())])
        assert by_ids == [{"session_id": session_id, "owner": owner, "repo": repo}], by_ids

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

        # awaiting_gate (Part 3 Task 1): set True right where a real gate_node would, immediately
        # before its interrupt() call would pause the graph; update_current_stage must clear it
        # unconditionally once that same stage is marked approved -- the exact "still drafting X"
        # vs. "paused at X's gate" distinction current_stage alone cannot carry (see
        # 0004_create_projects.sql's own comment for the full trace).
        await set_awaiting_gate(session_id, True)
        row = await get_session(session_id)
        assert row["awaiting_gate"], row

        await update_current_stage(session_id, "tech-stack")
        row = await get_session(session_id)
        assert row["current_stage"] == "tech-stack", row
        assert not row["awaiting_gate"], row  # cleared by that same UPDATE

        await close_session(
            session_id, run_id="r3", status="failed",
            failure={"stage": "exit", "type": "gates_not_passed", "feedback": "missing screenshots"},
        )
        row = await get_session(session_id)
        assert row["status"] == "failed" and row["failure_message"] == "missing screenshots", row
        assert row["ended_at"] is not None, row

        # Resume: touch_run must clear the stale failure/pr fields from the failed attempt above,
        # and (Part 3 Task 1) a stale awaiting_gate left behind by a process restart mid-pause --
        # the in-memory checkpoint that would have resumed straight into that gate is gone, so the
        # next round drafts again rather than sitting at the old gate.
        await set_awaiting_gate(session_id, True)
        await touch_run(session_id, run_id="r4", title="Initial request, revised, again")
        row = await get_session(session_id)
        assert row["status"] == "in_progress", row
        assert row["ended_at"] is None, row
        assert row["failure_stage"] is None and row["failure_message"] is None, row
        assert not row["awaiting_gate"], row

        await close_session(
            session_id, run_id="r4", status="completed",
            merge_ready=True, pr_title="Add the thing", pr_url="https://github.com/o/r/pull/1",
        )
        row = await get_session(session_id)
        assert row["status"] == "completed" and row["pr_url"] == "https://github.com/o/r/pull/1", row

        sessions = await list_sessions(owner, repo)
        assert any(s["session_id"] == session_id for s in sessions), sessions

        # project_id filter (Part 3 Task 1): scoping to the real project keeps this session, an
        # unrelated project_id excludes it.
        scoped = await list_sessions(owner, repo, project_id=project_id)
        assert any(s["session_id"] == session_id for s in scoped), scoped
        other_project = await list_sessions(owner, repo, project_id=str(uuid.uuid4()))
        assert not any(s["session_id"] == session_id for s in other_project), other_project

        await delete_session(session_id)
        assert await get_session(session_id) is None

        print("session_store self-check: ok")
    finally:
        # Idempotent: the assertions above already deleted the row on the success path, so this
        # is only reached (and only matters) if an earlier assertion raised first.
        await delete_session(session_id)
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM dbo.projects WHERE project_id = ?", project_id)


async def _demo_and_close() -> None:
    await _demo()
    pool = await _get_pool()
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.session_store
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo_and_close())
