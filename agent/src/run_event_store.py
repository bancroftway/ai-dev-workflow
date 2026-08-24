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
import time
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


async def append_events(events: list[RunEvent]) -> list[RunEvent]:
    """Batch counterpart to append_event (Phase E audit finding 5): one multi-row INSERT for a
    whole turn's worth of events instead of N sequential round-trips. Both chat models' per-tool-
    call translation loops (claude_chat_model.py, copilot_chat_model.py) call this once per turn
    with the whole translated list, then still call run_event_stream.emit_live per event
    afterward -- that dispatch is in-process, not a DB write, so the Spec's "batched... not one
    write per X" requirement is about THIS function, not about making emit_live batch too.

    Same fail-soft-swallow contract as append_event (its own docstring): a DB blip must never
    abort the LLM-cost-incurring node that produced these events. On failure, returns `events`
    unchanged (seq/ts left as whatever the caller passed in, normally None) instead of raising.

    Empty list in, empty list out, no query issued -- an empty VALUES clause is invalid SQL and
    there is nothing to batch.

    Row-to-event correlation: the returned rows are sorted by `seq` ascending before zipping
    against `events` in the caller's own list order. This is deliberately NOT "trust whatever
    order OUTPUT/fetchall() hand back" -- Microsoft's own docs do not contractually guarantee the
    OUTPUT clause preserves row order for a multi-row statement. What IS relied on instead: a
    plain `INSERT ... VALUES (...), (...), ...` assigns IDENTITY values to rows in the literal
    listed order (a row-constructor scan, not a re-orderable query plan the way INSERT...SELECT
    with no ORDER BY would be) -- so sorting the OUTPUT rows by the IDENTITY column (`seq`) itself
    recovers the original VALUES order regardless of what order they came back in. Verified
    empirically against this project's own real local SQL Server (ODBC Driver 18) before writing
    this, not assumed from docs alone: a 10-row batch, repeated across 4 trials, came back in
    VALUES order every time either way -- sorting by seq is the belt-and-braces version of an
    already-observed-correct behavior, not a defense against an observed failure.
    """
    if not events:
        return []
    try:
        pool = await _get_pool()
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?)"] * len(events))
        params: list[Any] = []
        for event in events:
            params += [
                event.run_id,
                event.session_id,
                event.stage,
                event.node,
                event.type.value,
                event.summary,
                json.dumps(event.payload) if event.payload is not None else None,
                json.dumps(event.token_usage) if event.token_usage is not None else None,
            ]
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO dbo.run_events (run_id, session_id, stage, node, type, summary, payload, token_usage)
                OUTPUT INSERTED.seq, INSERTED.ts
                VALUES {placeholders}
                """,
                *params,
            )
            rows = await cur.fetchall()
        ordered = sorted(rows, key=lambda row: row[0])  # by seq -- see docstring for why
        return [replace(event, seq=row[0], ts=row[1]) for event, row in zip(events, ordered)]
    except Exception:  # noqa: BLE001 -- best-effort instrumentation; never abort the node/run over this
        logger.warning(
            "append_events failed for %d event(s) starting run_id=%s -- continuing without it",
            len(events), events[0].run_id, exc_info=True,
        )
        return events


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


async def list_events_by_session(session_id: str) -> list[RunEvent]:
    """Oldest-first, across EVERY run_id this session has ever had -- not just its current one.

    Part 2 Task 8 (the new `GET /sessions/{session_id}/events` route, sessions_api.py) needs "this
    session's full event history," and keying that by session_id rather than run_id is a deliberate
    choice, not an arbitrary one: 0006_create_run_events.sql's own column comment spells out that
    `sessions.run_id` "remints across resumes" -- confirmed in session_store.touch_run, which mints
    a genuinely new run_id whenever a resume carries a revised title (`refresh_identity`) -- so a
    session that failed, got resumed, and ran again has MORE THAN ONE run_id in its lifetime, and
    dbo.sessions only ever stores the current one. Looking up that current run_id first and calling
    list_events(run_id) above would silently drop every event from a prior attempt on resume -- the
    exact durability this table exists for (outliving a torn-down sandbox) would then not actually
    outlive a resume either. session_id, unlike run_id, is the one stable identifier a run_events
    row always carries (NOT NULL, FK'd to dbo.sessions) for this session's entire lifetime, so
    filtering on it directly is correct where list_events's run_id filter would not be.

    Ordering by seq is still correct here for the same reason list_events's own docstring gives:
    seq is one monotonic, table-wide IDENTITY counter, so "oldest first" holds across a session's
    several run_ids exactly as it does within a single one -- no separate per-run_id merge/sort
    needed.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM dbo.run_events WHERE session_id = ? ORDER BY seq ASC",
            session_id,
        )
        rows = await cur.fetchall()
        return [_row_to_event(row) for row in rows]


async def delete_events_by_session(session_id: str) -> None:
    """Deletes every row this session has ever had, across all its run_ids -- same session_id
    scoping as list_events_by_session above, for the opposite direction.

    Required before a caller can delete the dbo.sessions row itself: 0006_create_run_events.sql's
    `session_id` column is `NOT NULL REFERENCES dbo.sessions(session_id)`, no `ON DELETE CASCADE`,
    so a session that has ever emitted a single real RunEvent (any sandboxed node run, or Part 2's
    own live-verification seeding technique) makes `DELETE FROM dbo.sessions` fail outright with a
    REFERENCE constraint violation otherwise -- confirmed live via sessions_api.delete_session_full
    (Task 14's real end-to-end sweep), not a theoretical gap: run_event_store._demo()'s own cleanup
    already deletes in this same run_events-before-sessions order for exactly this reason, but that
    discipline had never been carried into the actual production "Delete session" endpoint.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.run_events WHERE session_id = ?", session_id)


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
        # other_run_id's event is ALSO under `session_id` -- deliberately the exact real scenario
        # list_events_by_session below exists for (a different run_id under the SAME session_id,
        # which is exactly what a resume mints; see that function's own docstring), not just a
        # throwaway "unrelated" fixture.
        other_run_id = uuid.uuid4().hex[:8]
        other_event = await append_event(RunEvent(
            run_id=other_run_id, session_id=session_id, type=RunEventType.NODE_FINISHED,
            stage="specification", node="draft", summary="unrelated run",
        ))

        events = await list_events(run_id)
        assert [e.seq for e in events] == [appended.seq, second.seq], events
        assert events[1].payload == {"audit_findings_count": 0, "audit_skipped_infra": False}, events[1]
        assert events[1].token_usage is None, events[1]

        # list_events_by_session (Part 2 Task 8): unlike list_events(run_id) just above (which
        # correctly excludes other_event), list_events_by_session(session_id) must include all
        # three events, oldest first -- proving a session's full history survives a run_id remint
        # instead of silently losing the pre-resume attempt's events.
        by_session = await list_events_by_session(session_id)
        assert [e.seq for e in by_session] == [appended.seq, second.seq, other_event.seq], by_session
        assert {e.run_id for e in by_session} == {run_id, other_run_id}, by_session

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

        # --- Phase E audit finding 5: append_events batch counterpart + Verification 11 ---
        #
        # Correctness first: a real 5-row batch must round-trip seq/ts per event in the SAME
        # order as the input list -- see append_events' own docstring for why sorting the
        # returned rows by seq, rather than trusting fetchall()'s raw order, is what actually
        # makes this safe (Microsoft does not contractually guarantee OUTPUT's row order).
        batch_run_id = uuid.uuid4().hex[:8]
        small_batch = [
            RunEvent(
                run_id=batch_run_id, session_id=session_id, type=RunEventType.TOOL_CALL,
                stage="specification", node="draft", summary=f"tool call: probe-{i}", payload={"i": i},
            )
            for i in range(5)
        ]
        appended_batch = await append_events(small_batch)
        assert len(appended_batch) == 5, appended_batch
        assert all(e.seq is not None and e.ts is not None for e in appended_batch), appended_batch
        assert [e.payload["i"] for e in appended_batch] == list(range(5)), (
            "batch results must line up with the input list's own order, not DB-return order"
        )
        assert [e.seq for e in appended_batch] == sorted(e.seq for e in appended_batch), (
            "seq should come back monotonically increasing in input order for one batch"
        )
        fetched_batch = await list_events(batch_run_id)
        assert [e.payload["i"] for e in fetched_batch] == list(range(5)), fetched_batch

        # Same fail-soft contract as append_event's own check just above.
        real_get_pool_for_batch = _get_pool

        async def _broken_pool_batch() -> aioodbc.Pool:
            raise RuntimeError("simulated DB outage")

        _get_pool = _broken_pool_batch
        try:
            unwritten = [RunEvent(run_id=batch_run_id, session_id=session_id, type=RunEventType.TOOL_CALL, summary="x")]
            broken_result = await append_events(unwritten)
            assert broken_result == unwritten, broken_result  # unchanged, not raised
        finally:
            _get_pool = real_get_pool_for_batch
        assert await append_events([]) == [], "empty input must short-circuit -- no query issued"

        # Verification 11 (the Spec's own throughput/batching requirement -- "confirm the chosen
        # transport actually carries the... throughput... at the batching interval chosen" --
        # never previously performed; Phase E audit finding 5 names it explicitly unperformed).
        # ~200 REAL RunEvents through the REAL store against the REAL local DB: serial
        # append_event vs one append_events batch. Real numbers recorded in fix-e3a-report.md,
        # not just here. This assertion only pins the DIRECTION (batch not slower than serial) --
        # absolute wall-clock varies by machine/DB load and has no business being a hardcoded
        # threshold in a self-check.
        chatty_run_serial = uuid.uuid4().hex[:8]
        chatty_run_batch = uuid.uuid4().hex[:8]
        chatty_events = [
            RunEvent(
                run_id=chatty_run_serial, session_id=session_id, type=RunEventType.TOOL_CALL,
                stage="minimal-code-to-green", node="draft", summary=f"tool call: Bash #{i}",
                payload={"command": f"echo {i}", "i": i},
            )
            for i in range(200)
        ]
        serial_start = time.monotonic()
        for event in chatty_events:
            await append_event(event)
        serial_elapsed = time.monotonic() - serial_start

        batch_events = [replace(event, run_id=chatty_run_batch) for event in chatty_events]
        batch_start = time.monotonic()
        await append_events(batch_events)
        batch_elapsed = time.monotonic() - batch_start

        speedup = (serial_elapsed / batch_elapsed) if batch_elapsed else float("inf")
        print(
            f"run_event_store Verification 11: serial append_event x200 = {serial_elapsed:.3f}s, "
            f"batch append_events x200 = {batch_elapsed:.3f}s ({speedup:.1f}x)"
        )
        assert batch_elapsed < serial_elapsed, (
            f"batch ({batch_elapsed:.3f}s) should not be slower than 200 serial round-trips "
            f"({serial_elapsed:.3f}s) -- Verification 11 regression"
        )

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
