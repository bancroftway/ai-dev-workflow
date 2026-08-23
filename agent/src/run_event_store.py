"""Durable per-run event store (SQL Server, `agent/db/migrations/0006_create_run_events.sql`) --
the durable counterpart to `repo_files.append_ledger_entry`'s ephemeral JSON-lines ledger, which
lives only inside the sandbox's own workspace and is gone once that sandbox is torn down. Part 2
(run-visibility UI redesign) needs history that survives that teardown; graph.py's draft/audit/
verify nodes call `append_event` right alongside their existing `append_ledger_entry` call, same
data, second destination -- additive, the ledger write is untouched.

Mirrors session_store.py's shape: plain async module-level functions over a shared aioodbc pool,
a `_COLUMNS` list, no class. `seq`/`ts` are DB-assigned (IDENTITY + SYSUTCDATETIME default, see the
migration's own comment for why seq is a table-wide counter and not reset per run_id) -- callers
never set them; `append_event` returns a copy of the given RunEvent with both filled in.

Self-check runs against a real DB (local or Azure, whichever `db.py` resolves to): `cd agent &&
uv run python -m src.run_event_store`.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from typing import Any

import aioodbc

from . import db
from .run_events import RunEvent, RunEventType

logger = logging.getLogger(__name__)

_pool: aioodbc.Pool | None = None


async def _get_pool() -> aioodbc.Pool:
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(autocommit=True, **db.connection_kwargs())
    return _pool


async def append_event(event: RunEvent) -> RunEvent:
    """Inserts one event row. Any seq/ts already set on `event` is ignored -- the DB assigns both;
    the returned copy carries what was actually stored.

    Best-effort, on purpose: this is non-critical instrumentation riding alongside
    repo_files.append_ledger_entry (graph.py's real call sites write both, additively). Unlike that
    ledger write, a failure here must never propagate -- an unhandled exception out of a node aborts
    the whole graph invocation (telemetry.traced_node), which would mean a transient DB blip on this
    new, purely-observational write kills an entire in-flight, LLM-cost-incurring run. Mirrors
    _with_live_refresh's (graph.py) same swallow-and-log shape, for the same reason ("a display
    refresh must never fail a real node"). Centralized here, not at each call site, so every current
    and future caller gets it for free. On failure, returns `event` unchanged (seq/ts stay whatever
    the caller passed in, normally None) instead of raising.
    """
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dbo.run_events (run_id, session_id, stage, node, type, summary, payload, token_usage)
                OUTPUT INSERTED.seq, INSERTED.ts
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event.run_id,
                event.session_id,
                event.stage,
                event.node,
                event.type.value,
                event.summary,
                json.dumps(event.payload) if event.payload is not None else None,
                json.dumps(event.token_usage) if event.token_usage is not None else None,
            )
            seq, ts = await cur.fetchone()
        return replace(event, seq=seq, ts=ts)
    except Exception:  # noqa: BLE001 -- best-effort instrumentation; never abort the node/run over this
        logger.warning(
            "append_event failed for run_id=%s stage=%s node=%s -- continuing without it",
            event.run_id, event.stage, event.node, exc_info=True,
        )
        return event


_COLUMNS = ["seq", "run_id", "session_id", "ts", "stage", "node", "type", "summary", "payload", "token_usage"]


def _row_to_event(row: Any) -> RunEvent:
    values = dict(zip(_COLUMNS, row))
    return RunEvent(
        run_id=values["run_id"],
        # UNIQUEIDENTIFIER comes back an uppercase string -- normalize like session_store._row_to_dict
        # does, so it matches the lowercase uuid4() every caller (frontend included) compares against.
        session_id=str(values["session_id"]).lower(),
        type=RunEventType(values["type"]),
        stage=values["stage"],
        node=values["node"],
        summary=values["summary"],
        payload=json.loads(values["payload"]) if values["payload"] is not None else None,
        token_usage=json.loads(values["token_usage"]) if values["token_usage"] is not None else None,
        seq=values["seq"],
        ts=values["ts"],
    )


async def list_events(run_id: str) -> list[RunEvent]:
    """Oldest-first. Ordering by seq is correct for a single run_id even though seq itself is a
    table-wide IDENTITY, not reset per run -- see the migration's own comment."""
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dbo.run_events WHERE run_id = ? ORDER BY seq ASC",
            run_id,
        )
        rows = await cur.fetchall()
        return [_row_to_event(row) for row in rows]


async def _demo() -> None:
    """Self-check against a real DB: `cd agent && uv run python -m src.run_event_store`."""
    global _get_pool  # reassigned further down (fail-soft check); must precede every use in this function
    project_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    run_id = uuid.uuid4().hex[:8]
    pool = await _get_pool()
    # dbo.run_events.session_id is a real FK to dbo.sessions (itself FK'd to dbo.projects) -- raw
    # SQL for both throwaway parent rows, same reasoning session_store._demo() gives for its own
    # throwaway project row: keeps this module's self-check independent of a sibling module's API.
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO dbo.projects (project_id, name, created_by) VALUES (?, ?, ?)",
            project_id, "run-event-store-selfcheck-project", "octocat",
        )
        await cur.execute(
            """
            INSERT INTO dbo.sessions
                (session_id, owner, repo, user_login, title, source_branch, work_branch, project_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')
            """,
            session_id, "octocat", "demo-repo-run-event-store-selfcheck", "octocat", "t",
            "main", f"ai-dev-workflow/{session_id}", project_id,
        )
    try:
        event = RunEvent(
            run_id=run_id,
            session_id=session_id,
            type=RunEventType.NODE_FINISHED,
            stage="specification",
            node="draft",
            summary="draft ready for review",
            payload={"readiness": True},
            token_usage={"model": "test-model", "input_tokens": 10, "output_tokens": 5, "cost": 0.001},
        )
        assert event.seq is None and event.ts is None, event  # not yet appended

        appended = await append_event(event)
        assert appended.seq is not None and appended.ts is not None, appended
        # Round-trips unchanged: every field the caller supplied comes back untouched; only seq/ts
        # (DB-assigned, deliberately unset going in) differ from the original.
        assert replace(appended, seq=None, ts=None) == event, (appended, event)

        events = await list_events(run_id)
        assert len(events) == 1 and events[0] == appended, events

        # A second event for the same run_id, plus one for an unrelated run_id -- list_events must
        # return only this run's events, oldest first, and a payload-less/token_usage-less event
        # (verify_node's real shape -- no LLM call, so no usage) must round-trip its Nones too.
        second = await append_event(RunEvent(
            run_id=run_id, session_id=session_id, type=RunEventType.NODE_FINISHED,
            stage="specification", node="audit", summary="audit found 0 finding(s)",
            payload={"audit_findings_count": 0, "audit_skipped_infra": False},
        ))
        other_run_id = uuid.uuid4().hex[:8]
        await append_event(RunEvent(
            run_id=other_run_id, session_id=session_id, type=RunEventType.NODE_FINISHED,
            stage="specification", node="draft", summary="unrelated run",
        ))

        events = await list_events(run_id)
        assert [e.seq for e in events] == [appended.seq, second.seq], events
        assert events[1].payload == {"audit_findings_count": 0, "audit_skipped_infra": False}, events[1]
        assert events[1].token_usage is None, events[1]

        # Fail-soft contract (coordinator review fix): a DB failure inside append_event must never
        # raise into the caller -- it logs a warning and hands back the original event unchanged, so
        # a transient blip can never abort the LLM-cost-incurring node that called it. Simulated by
        # swapping _get_pool for one that always raises, same "plain global reassignment" technique
        # sessions_api._demo() uses for its own monkeypatches.
        real_get_pool = _get_pool

        async def _broken_pool() -> aioodbc.Pool:
            raise RuntimeError("simulated DB outage")

        _get_pool = _broken_pool
        try:
            broken = RunEvent(
                run_id=run_id, session_id=session_id, type=RunEventType.NODE_FINISHED,
                stage="specification", node="draft", summary="should not persist",
            )
            result = await append_event(broken)
            assert result == broken, result  # unchanged, not raised
        finally:
            _get_pool = real_get_pool

        print("run_event_store self-check: ok")
    finally:
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM dbo.run_events WHERE session_id = ?", session_id)
            await cur.execute("DELETE FROM dbo.sessions WHERE session_id = ?", session_id)
            await cur.execute("DELETE FROM dbo.projects WHERE project_id = ?", project_id)


async def _demo_and_close() -> None:
    await _demo()
    pool = await _get_pool()
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_event_store
    import asyncio

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_demo_and_close())
