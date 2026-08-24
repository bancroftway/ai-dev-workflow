"""Deploy-time drain (Phase E audit I-5, Ruling E-2: option (b) -- accept orphaning, make it
legible instead of silent).

The Part 1 Spec asked an explicit question this branch never answered anywhere: what happens to a
session already mid-run when a deploy restarts the agent process? The honest answer, unavoidable
given this pipeline's own architecture, is that it gets orphaned: `graph.py`'s compiled graph uses
`InMemorySaver` (see `GraphState.provider`'s own comment), which holds every run's `stages`,
`run_id`, and pinned `provider` in process memory ONLY -- a restart drops all of it, for every
in-flight thread, with nothing durable left to resume into. The sandbox container itself can
survive the restart (it is reaped on its own idle clock, independent of the agent process), so
without this module a user's ticket just sits there silently: no error, no banner, the board still
shows "in progress," and the only way to discover it is stale is to poke it and watch it fail
strangely on reattach.

This module is the "make it legible" half the Spec called for: enumerate every session the DB
still calls `in_progress` and NOT currently paused at a human gate, and mark each one failed with a
plain, user-visible reason ("interrupted by deploy -- resubmit to retry") instead of leaving it to
fail confusingly later. A drain WINDOW (Spec option (a) -- stop accepting new sessions, wait for
in-flight ones to finish) was considered and rejected as the larger option: it needs a "stop
accepting new work" flag this codebase has no mechanism for today, for a benefit (zero interrupted
runs) this option (b) doesn't need either, since every interrupted run already has a clear,
actionable, resubmit-and-retry path.

One plain DB query, no sandbox provider, no in-memory registry (review round 1 fix: the first
version of this module read `SandboxProvider.list_active()`, an in-memory dict scoped to the
process that provisioned each sandbox -- structurally unable to see anything when run as a freshly
spawned, separate process after the old agent process has already exited, which is exactly how a
deploy step invokes it. Querying `dbo.sessions` directly instead needs no in-process state at all,
so `python -m src.deploy_drain --run` is now genuinely functional as an ordinary, detached deploy
step -- no special "must run inside the old process" caveat, unlike the first version):

    SELECT session_id, run_id, current_stage FROM dbo.sessions
    WHERE status = 'in_progress' AND awaiting_gate = 0

`dbo.sessions.status`'s own CHECK constraint (`0001_create_sessions.sql`) closes the vocabulary to
`('in_progress','completed','failed','rejected')`, so `in_progress` is the only non-terminal value
-- nothing else needs excluding on that axis. No container-liveness cross-check either: a sandbox
that has already been reaped only makes an in_progress-and-not-awaiting-gate row MORE certainly
orphaned, never less, so there is nothing a liveness probe could add here.

**Why `awaiting_gate = 1` is excluded** (review round 2): a session paused at a human gate has a
real recovery path a plain in-progress one doesn't -- `session_store.set_awaiting_gate`'s own
docstring, and `workflow_persistence.hydrate_state`/`persist_state` restore an approved stage's
content from the sandbox's own `.ai-dev-workflow/*.approved.json` on the next intake regardless of
whether the in-memory checkpoint survived. Failing a human-waiting queue on every deploy -- when the
human might approve five minutes later and the run would otherwise continue exactly where it left
off -- would make the mitigation worse than the problem for that population. Verified, not merely
inferred, for the PENDING-DRAFT half specifically (the part the review asked to check rather than
assume): `graph.py`'s `_persist_if_sandboxed` is called from `make_audit_node` (after the audit
pass) and from `make_verify_node` (on a passing `deterministic_verify`) -- both BEFORE `gate_node`
ever reaches `interrupt()` -- for every stage that has an audit pass or a deterministic_verify.
Grepping `graph.py`'s own `STAGES` list: every stage from `specification` through `metrics-exit` has
at least one of the two. The lone exception is `tech-stack` -- the only StageSpec with neither
`audit_response_schema` nor `deterministic_verify` set. For a tech-stack draft that reached the gate
via a genuinely fresh LLM detection (no `hydrate_from_repo_file`/`prefill_from_repo_file` match --
a real, common case: any Connect-Repository/brownfield project's first-ever tech-stack stage, with
no committed `tech-stack.md` and no ticket-time picker selection either), NOTHING persists that
draft to `.ai-dev-workflow/*.draft.json` before `interrupt()` pauses -- confirmed by reading
`gate_node`'s own body, which writes only the durable `awaiting_gate` flag and a `GATE_PAUSED`
event, never the draft content, immediately before calling `interrupt()`. A backend restart while
THAT specific gate is paused genuinely loses the pending draft; the next intake simply re-runs
tech-stack detection from scratch (cheap and close to idempotent, not a lost user submission -- the
human's own Raw Requirements Text, which is what they actually typed, is unaffected). Excluding
`awaiting_gate = 1` unconditionally is still the right call in aggregate -- one stage's worst case
is "redo a deterministic scan," versus failing a real human-waiting queue (including every OTHER
stage's gate, where nothing is lost) on every single deploy -- but it is a known, narrow, disclosed
gap in the "every gate-paused session has a recovery path" premise, not a universal guarantee.

Usage: `cd agent && uv run python -m src.deploy_drain` runs the self-check against a real DB (safe:
scoped to its own seeded rows, see `_demo` below -- matches every other module in this package's
"bare invocation runs the self-check" convention). `cd agent && uv run python -m src.deploy_drain
--run` performs a REAL drain -- a plain, ordinary deploy step, no process-locality caveat.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from . import session_store

logger = logging.getLogger(__name__)

# Plain, user-visible -- this is what a user sees as this session's failure_message (session
# detail page/board card), not a log line only an operator would ever read.
INTERRUPTED_BY_DEPLOY_MESSAGE = "interrupted by deploy -- resubmit to retry"


async def _list_drainable_sessions() -> list[dict[str, Any]]:
    """The one query this module needs: every session still `in_progress` and not paused at a
    human gate. See the module docstring for why both predicates are exactly right and nothing
    else (container liveness included) needs checking."""
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package, same reuse convention project_store.py/org_settings.py already use for session_store's own pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        # awaiting_gate is `BIT NULL` with no column default (0004_create_projects.sql) -- a
        # freshly created session that has never reached a gate is NULL, not 0, and SQL's
        # three-valued logic means `awaiting_gate = 0` alone silently excludes it (`NULL = 0` is
        # UNKNOWN, never TRUE). Treat NULL the same as 0/not-awaiting, matching every other reader
        # in this codebase (session_store._row_to_response, gate_node's own
        # `bool(existing_session and existing_session.get("awaiting_gate"))`).
        await cur.execute(
            "SELECT session_id, run_id, current_stage FROM dbo.sessions "
            "WHERE status = 'in_progress' AND (awaiting_gate = 0 OR awaiting_gate IS NULL)"
        )
        rows = await cur.fetchall()
        # SQL Server hands UNIQUEIDENTIFIER back uppercase -- normalize to lowercase, same
        # convention session_store._row_to_dict already applies for every other reader.
        return [{"session_id": str(r[0]).lower(), "run_id": r[1], "current_stage": r[2]} for r in rows]


async def drain(
    *,
    list_drainable: Callable[[], Awaitable[list[dict[str, Any]]]] = _list_drainable_sessions,
    close_session: Callable[..., Awaitable[None]] = session_store.close_session,
) -> list[str]:
    """Marks every session `list_drainable` names as `failed`, with `INTERRUPTED_BY_DEPLOY_MESSAGE`
    as the reason. Returns the session_ids actually marked.

    list_drainable/close_session default to the real functions -- overridable so this module's own
    self-check (`_demo` below) can scope which rows it touches to its own seeded fixture, without
    which running the REAL, unfiltered query during a self-check could drain a genuinely in-progress
    session that happens to exist in whatever DB the check runs against.
    """
    rows = await list_drainable()
    marked: list[str] = []
    for row in rows:
        session_id = row["session_id"]
        await close_session(
            session_id,
            run_id=row.get("run_id"),
            status="failed",
            failure={
                "stage": row.get("current_stage"),
                "type": "interrupted_by_deploy",
                "feedback": INTERRUPTED_BY_DEPLOY_MESSAGE,
            },
        )
        marked.append(session_id)
        logger.info("deploy_drain: marked session_id=%s as failed (%s)", session_id, INTERRUPTED_BY_DEPLOY_MESSAGE)
    return marked


async def _demo() -> None:
    """Self-check against a real DB: `cd agent && uv run python -m src.deploy_drain`. Seeds three
    real dbo.sessions rows in the shapes that matter (in_progress+not-awaiting -> drained;
    in_progress+awaiting_gate -> untouched; completed -> untouched), runs the REAL query and the
    REAL drain(), and asserts. Safe against a populated real DB: the candidate list `drain()` acts
    on is the real `_list_drainable_sessions()` output post-filtered to this fixture's own three
    session_ids, so an unrelated real in-progress session in the same database is never touched --
    the real WHERE clause is still exercised for real, just not blindly trusted with the whole
    table during a self-check."""
    import uuid

    from . import project_store

    project_id = await project_store.create_project(
        "deploy-drain-selfcheck-project", tech_stack_id=None, tech_stack_text=None, created_by="octocat"
    )
    drainable_id, gated_id, completed_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    fixture_ids = {drainable_id, gated_id, completed_id}
    try:
        for session_id in fixture_ids:
            await session_store.create_session(
                session_id, owner="octocat", repo="deploy-drain-selfcheck-repo", user_login="octocat",
                source_branch="main", work_branch=f"ai-dev-workflow/{session_id}", title="t",
                project_id=project_id,
            )
        await session_store.set_awaiting_gate(gated_id, True)
        await session_store.close_session(completed_id, run_id="rdone", status="completed")

        # Proves the real SQL predicate directly, against real rows, before drain() ever runs:
        # the awaiting-gate and completed rows must not even be candidates.
        raw = await _list_drainable_sessions()
        raw_ids = {r["session_id"] for r in raw}
        assert drainable_id in raw_ids, "the plain in_progress+not-awaiting row must be a candidate"
        assert gated_id not in raw_ids, "an awaiting_gate=1 row must never be a candidate"
        assert completed_id not in raw_ids, "a completed row must never be a candidate"

        async def _scoped_list() -> list[dict[str, Any]]:
            return [r for r in raw if r["session_id"] in fixture_ids]

        marked = await drain(list_drainable=_scoped_list)

        assert marked == [drainable_id], (
            f"only the in_progress+not-awaiting session must be drained, got {marked}"
        )

        drained_row = await session_store.get_session(drainable_id)
        assert drained_row["status"] == "failed", drained_row
        assert drained_row["failure_type"] == "interrupted_by_deploy", drained_row
        assert drained_row["failure_message"] == INTERRUPTED_BY_DEPLOY_MESSAGE, drained_row

        gated_row = await session_store.get_session(gated_id)
        assert gated_row["status"] == "in_progress", "a gate-paused session must be left untouched"

        completed_row = await session_store.get_session(completed_id)
        assert completed_row["status"] == "completed", "an already-terminal session must be left untouched"

        print("deploy_drain self-check: all assertions passed")
    finally:
        for session_id in fixture_ids:
            await session_store.delete_session(session_id)
        pool = await session_store._get_pool()  # noqa: SLF001
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM dbo.projects WHERE project_id = ?", project_id)


async def _run_for_real() -> None:  # pragma: no cover -- mutates real dbo.sessions rows
    marked = await drain()
    if marked:
        print(f"deploy_drain: marked {len(marked)} session(s) as interrupted-by-deploy: {', '.join(marked)}")
    else:
        print("deploy_drain: no drainable (in_progress, not awaiting a gate) sessions found")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.deploy_drain [--run]
    import asyncio
    import sys

    logging.basicConfig(level=logging.INFO)
    if "--run" in sys.argv[1:]:
        asyncio.run(_run_for_real())
    else:
        # Default (no args), same convention as every other module in this package
        # (session_store.py, project_store.py, model_config.py, ...): run the self-check.
        # Re-dispatched through the PACKAGE name so this module isn't imported twice under two
        # different sys.modules identities, same reason those other modules' own __main__ blocks do.
        from src.deploy_drain import _demo as _packaged_demo

        asyncio.run(_packaged_demo())
