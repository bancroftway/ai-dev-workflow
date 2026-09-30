"""Per-session snapshot of `dbo.runtime_settings` (SQL Server, agent/db/migrations/
0021_create_runtime_settings.sql) -- the live-editable backing store `config.py`'s __getattr__
shim reads through, so any of its ~100+ constants can change via the Org Settings UI without a
redeploy.

Pinned once per session, never refreshed mid-session -- deliberately the SAME "pin once per run,
never drift" rule chat_model.py's `state["provider"]` already follows (Ruling 2), not a live
30s-TTL cache. An admin's edit reaches the NEXT session started, never an in-flight one.

The mechanism is a `contextvars.ContextVar`, not a shared process-global dict: `agent/main.py`'s
long-lived server drives MANY CONCURRENT sessions in one process, each its own `asyncio.Task`
(`_ReattachStateAgent._drive_graph`, one task per thread_id). `asyncio.create_task` copies the
calling code's context at creation time, and a value a task sets on its OWN context is visible to
everything that task later awaits into -- so `pin_for_session()` called once at the top of
`_drive_graph` correctly scopes that one session's snapshot to its own task, isolated from every
other concurrently-running session, with zero parameter-threading through the ~265 existing
`config.*` call sites.

Deliberately NOT called from `sessions_api.py`'s provisioning endpoint (where `chat_model.
get_provider()` is resolved for a NEW session) -- that HTTP request is a separate, earlier task
from `_drive_graph`'s own task, so a ContextVar set there would not propagate (provider avoids this
entirely by persisting its resolved value to `dbo.sessions.provider` and re-reading it explicitly
into GraphState; these settings are simpler scalars, not worth the same DB-persistence machinery,
so they just get pinned fresh inside `_drive_graph` itself instead).

`agent/run_headless.py` has no such task-boundary complication -- its whole process is one
straight-through coroutine, so `pin_for_session()` there is a single call near its own
`chat_model.get_provider()` resolution, no different task to worry about.

Self-check is offline only (no live DB in this environment, same limitation as org_settings.py's
own self-check): `cd agent && uv run python -m src.runtime_settings`.
"""

from __future__ import annotations

import contextvars
import logging

from . import session_store

logger = logging.getLogger(__name__)

# None = no session has pinned a snapshot in this task's context (never called pin_for_session(),
# e.g. a stray background task) -- treated identically to "pinned, but this key had no override."
_session_settings: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "runtime_settings_session", default=None
)


def get_raw(key: str) -> str | None:
    """The current session's resolved override for `key`, or None -- sync, zero I/O, safe to call
    from anywhere (the ~265 call sites this backs are plain, mostly-synchronous code). Returns None
    both when this session was never pinned and when it was pinned but `key` has no override --
    config.py's __getattr__ treats both identically (fall through to the env/default tier)."""
    snapshot = _session_settings.get()
    if snapshot is None:
        return None
    return snapshot.get(key)


async def _fetch_all() -> dict[str, str]:
    pool = await session_store._get_pool()  # noqa: SLF001 -- same package; one shared aioodbc pool
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT setting_key, setting_value FROM dbo.runtime_settings WHERE setting_value IS NOT NULL"
        )
        rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}


async def pin_for_session() -> None:
    """The ONE fetch for this session: reads every current override and pins it to this task's
    context for the rest of the session's lifetime. Call once, at the top of
    `_ReattachStateAgent._drive_graph` (agent/main.py) or once at run_headless.py's own startup --
    never per-call, never on a timer. A DB read failure here must not crash a session any more than
    a config.py env-var read ever could: log and pin an empty snapshot, which resolves identically
    to "no overrides" and falls through to config.py's env/default tier for everything."""
    try:
        snapshot = await _fetch_all()
    except Exception:
        logger.warning(
            "runtime_settings fetch failed; session proceeds on config.py's env/default tier only",
            exc_info=True,
        )
        snapshot = {}
    _session_settings.set(snapshot)


async def list_overrides() -> dict[str, dict[str, str | None]]:
    """Every current override straight from the DB -- for the Settings API's list endpoint, which
    must show live truth, not this session's pinned snapshot (an admin viewing the settings page
    needs to see another admin's just-saved change immediately, same "always a fresh, uncached
    read" contract sessions_api.py's own _org_settings_response() already follows for
    org_settings.py). Keyed by setting_key; value is {"value", "updated_by", "updated_at"} (the
    last as an ISO string, or None if the column is NULL)."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT setting_key, setting_value, updated_by, updated_at FROM dbo.runtime_settings "
            "WHERE setting_value IS NOT NULL"
        )
        rows = await cur.fetchall()
        return {
            row[0]: {
                "value": row[1],
                "updated_by": row[2],
                "updated_at": row[3].isoformat() if row[3] is not None else None,
            }
            for row in rows
        }


async def set_value(key: str, formatted_value: str, updated_by: str) -> None:
    """MERGE upsert against one row. `formatted_value` must already be in the exact string shape
    the setting's own parser expects (a CSV-style setting gets "a,b,c", a JSON-typed setting gets
    real JSON text) -- callers (the Settings API, task 7) run the setting's formatter before this,
    never json.dumps() an arbitrary Python value here. Takes effect for the NEXT session pinned,
    not any already-running one -- there is no cache to invalidate."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.runtime_settings AS target
            USING (SELECT ? AS setting_key) AS src
              ON target.setting_key = src.setting_key
            WHEN MATCHED THEN UPDATE SET setting_value = ?, updated_by = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (setting_key, setting_value, updated_by) VALUES (?, ?, ?);
            """,
            key,
            formatted_value, updated_by,
            key, formatted_value, updated_by,
        )


async def delete_value(key: str, updated_by: str) -> None:  # noqa: ARG001 -- updated_by kept for signature symmetry with set_value; DELETE has no audit column to write it into
    """Clear an override, reverting `key` to config.py's env-var/default tier for the next session
    pinned. Removes the row entirely rather than setting setting_value to NULL -- same "absent
    means no override" contract migration 0021 documents, one less state to reason about."""
    pool = await session_store._get_pool()  # noqa: SLF001
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.runtime_settings WHERE setting_key = ?", key)


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.runtime_settings`. No live DB in this
    environment -- exercises only the ContextVar contract (unpinned vs. pinned-but-unset vs.
    pinned-and-set), not the SQL itself. The MERGE/SELECT/DELETE statements are verified against a
    real DB the same way org_settings.py's own self-check defers its SQL verification."""
    # Unpinned (no pin_for_session() ever called in this context) -- must be None, never a
    # KeyError/AttributeError, so config.py's shim can unconditionally fall through.
    assert get_raw("SPEC_MAX_VERIFY_CYCLES") is None, get_raw("SPEC_MAX_VERIFY_CYCLES")

    # Pinned with a snapshot: the overridden key resolves, an absent key still falls through to
    # None (not a KeyError) so config.py's shim can treat "pinned but unset" identically to
    # "never pinned."
    token = _session_settings.set({"SPEC_MAX_VERIFY_CYCLES": "7"})
    try:
        assert get_raw("SPEC_MAX_VERIFY_CYCLES") == "7", get_raw("SPEC_MAX_VERIFY_CYCLES")
        assert get_raw("PLAN_MAX_VERIFY_CYCLES") is None, get_raw("PLAN_MAX_VERIFY_CYCLES")
    finally:
        _session_settings.reset(token)

    # Reset must actually restore "unpinned," not leave the prior snapshot behind.
    assert get_raw("SPEC_MAX_VERIFY_CYCLES") is None, get_raw("SPEC_MAX_VERIFY_CYCLES")

    # Pinned with an empty snapshot (DB reachable, table empty, or fetch failed) resolves
    # identically to "no overrides" for every key -- the exact day-1 no-op contract.
    token = _session_settings.set({})
    try:
        assert get_raw("SPEC_MAX_VERIFY_CYCLES") is None, get_raw("SPEC_MAX_VERIFY_CYCLES")
    finally:
        _session_settings.reset(token)

    print("runtime_settings self-check: ok (ContextVar contract only, no live DB in this environment)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.runtime_settings
    # Re-dispatch through the PACKAGE name on purpose -- same convention as org_settings.py/
    # chat_model.py: `python -m src.runtime_settings` loads this file as "__main__", so a direct
    # _demo() call would import this module a second time under a separate sys.modules identity,
    # splitting its module-level _session_settings ContextVar across two entries.
    from src.runtime_settings import _demo as _packaged_demo

    _packaged_demo()
