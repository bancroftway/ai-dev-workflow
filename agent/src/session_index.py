"""Durable per-repo session index at `.ai-dev-workflow/sessions.json` (schema_version 1) --
the record that lets `/select` show session history and a failed/in-progress run be resumed.

Read-modify-write with corrupt-file tolerance, same pattern as `preflight_nodes.update_manifest`:
a malformed file is replaced, never raised on mid-run. `_upsert`/`_close` are the pure halves
(no sandbox I/O), each covered by `_demo()` below -- runnable via `uv run python -m
src.session_index`.

Schema per entry: `{run_id, thread_id, title, user, target_branch, started_at, ended_at,
status: "in_progress"|"completed"|"failed"|"rejected"|"superseded", failure: {stage, type,
message}|None, exit: {merge_ready, pr_title}|None}`.

`user` is advisory only: it is whatever GitHub login the Next.js provision route forwarded
(sessions_api.py's `ProvisionRequest.user_login`), never verified agent-side.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import git_ops, repo_files
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

SESSIONS_PATH = ".ai-dev-workflow/sessions.json"
SCHEMA_VERSION = 1

_DEFAULT_RECENT_SESSIONS = 20


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_cap() -> int:
    """N = env AIDW_RECENT_SESSIONS x2 (default 40) -- double the list endpoint's display cap
    (Number(process.env.AIDW_RECENT_SESSIONS) || 20), so the file always holds enough headroom
    for the endpoint's own slice to have something to sort through."""
    try:
        configured = int(os.environ.get("AIDW_RECENT_SESSIONS") or _DEFAULT_RECENT_SESSIONS)
    except ValueError:
        configured = _DEFAULT_RECENT_SESSIONS
    return max(configured, 1) * 2


# --------------------------------------------------------------------------------------------
# Pure half -- no sandbox, no I/O, self-checked at the bottom of this module.
# --------------------------------------------------------------------------------------------


def _upsert(
    sessions: list[dict[str, Any]],
    *,
    thread_id: str,
    run_id: str,
    title: str,
    user: str,
    target_branch: str,
    started_at: str,
) -> list[dict[str, Any]]:
    """UPSERT-by-thread_id: one in_progress row per thread, never one per clarification round
    (scaffold_node runs once per round, not once per logical session -- a naive append would mint
    a zombie row per round, titled with that round's clarification answer instead of the original
    request).

    Returns a NEW list. The thread's in_progress row is updated in place; run_id/started_at/title
    only refresh together when `title` is non-empty and differs from the row's current title (a
    round that repeats the same title, or genuinely has none, keeps the original run's identity).
    Any OTHER in_progress row for the same thread_id is closed as "superseded". A thread with no
    in_progress row yet gets a brand new one appended.
    """
    result = [dict(s) for s in sessions]
    matches = [
        i for i, s in enumerate(result) if s.get("thread_id") == thread_id and s.get("status") == "in_progress"
    ]

    if not matches:
        result.append(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "title": title or f"(untitled run {run_id})",
                "user": user,
                "target_branch": target_branch,
                "started_at": started_at,
                "ended_at": None,
                "status": "in_progress",
                "failure": None,
                "exit": None,
            }
        )
        return result

    primary_idx = matches[0]
    for i in matches[1:]:
        result[i] = {**result[i], "status": "superseded", "ended_at": started_at}

    primary = dict(result[primary_idx])
    if title and title != primary.get("title"):
        primary["run_id"] = run_id
        primary["started_at"] = started_at
        primary["title"] = title
    primary["user"] = user
    primary["target_branch"] = target_branch
    result[primary_idx] = primary
    return result


def _build_failure(payload: dict[str, Any]) -> dict[str, Any]:
    """Raw run_failure payload (shape varies per escalate_node -- `report`/`feedback` are
    sometimes dicts, sometimes strings, sometimes absent) -> the {stage, type, message} triple
    the schema promises."""
    raw_message = payload.get("feedback") or payload.get("report") or ""
    return {
        "stage": payload.get("stage"),
        "type": payload.get("type"),
        "message": str(raw_message).strip()[:500],
    }


def _close(
    sessions: list[dict[str, Any]],
    *,
    thread_id: str,
    run_id: str | None,
    status: str,
    ended_at: str,
    failure: dict[str, Any] | None = None,
    exit_summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Sets ended_at/status/failure/exit on the matching entry: by (thread_id, run_id) first,
    falling back to the thread's in_progress entry when run_id is absent or doesn't match (e.g. a
    failure escalated before intake ever minted this run's row). A no-op list when nothing
    matches -- the caller's own commit is a no-op too, log-and-continue rather than raise."""
    result = [dict(s) for s in sessions]
    idx = next(
        (i for i, s in enumerate(result) if s.get("thread_id") == thread_id and run_id and s.get("run_id") == run_id),
        None,
    )
    if idx is None:
        idx = next(
            (i for i, s in enumerate(result) if s.get("thread_id") == thread_id and s.get("status") == "in_progress"),
            None,
        )
    if idx is None:
        logger.info("session_index: no matching entry to close for thread_id=%s run_id=%s", thread_id, run_id)
        return result

    entry = dict(result[idx])
    entry["status"] = status
    entry["ended_at"] = ended_at
    entry["failure"] = failure
    entry["exit"] = exit_summary
    result[idx] = entry
    return result


def _cap(sessions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keeps the file from growing forever, WITHOUT ever evicting an in_progress row.

    Plain index truncation (drop the front) breaks that: rows never move once appended, so a
    stalled in_progress session near the front gets silently truncated away after ~limit more
    sessions start elsewhere -- end_session then finds no matching entry, logs it, and the
    outcome is lost for good. Keep every in_progress row regardless of position, plus the most
    recent `limit` CLOSED rows (completed/failed/rejected/superseded), reassembled in original
    (chronological) order.
    """
    if limit <= 0 or len(sessions) <= limit:
        return sessions
    closed = [s for s in sessions if s.get("status") != "in_progress"]
    kept_closed_ids = {id(s) for s in (closed[-limit:] if len(closed) > limit else closed)}
    return [s for s in sessions if s.get("status") == "in_progress" or id(s) in kept_closed_ids]


# --------------------------------------------------------------------------------------------
# Sandbox-I/O half.
# --------------------------------------------------------------------------------------------


async def _read(provider: SandboxProvider, thread_id: str) -> list[dict[str, Any]]:
    raw = await repo_files.read_repo_file(provider, thread_id, SESSIONS_PATH)
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("sessions.json is not valid JSON for thread_id=%s; replacing it", thread_id)
        return []
    if not isinstance(doc, dict):
        return []
    sessions = doc.get("sessions")
    return sessions if isinstance(sessions, list) else []


async def _write(provider: SandboxProvider, thread_id: str, sessions: list[dict[str, Any]]) -> None:
    doc = {"schema_version": SCHEMA_VERSION, "sessions": sessions}
    await repo_files.write_repo_file(provider, thread_id, SESSIONS_PATH, json.dumps(doc, indent=2) + "\n")


async def start_session(
    provider: SandboxProvider,
    thread_id: str,
    *,
    run_id: str,
    title: str,
    user: str,
    target_branch: str,
) -> None:
    """UPSERTs this thread's in_progress row and commits+pushes sessions.json immediately, so the
    entry survives a crash before anything else in the run commits (scaffold_node calls this
    before its own baseline-commit capture, specifically so a reject's later `git reset --hard`
    to that baseline keeps this commit rather than erasing it)."""
    sessions = await _read(provider, thread_id)
    sessions = _upsert(
        sessions,
        thread_id=thread_id,
        run_id=run_id,
        title=title,
        user=user,
        target_branch=target_branch,
        started_at=_now(),
    )
    sessions = _cap(sessions, _file_cap())
    await _write(provider, thread_id, sessions)
    await git_ops.commit_paths(provider, thread_id, [SESSIONS_PATH], f"ai-dev-workflow: session {run_id} started")


async def end_session(
    provider: SandboxProvider,
    thread_id: str,
    *,
    run_id: str | None,
    status: str,
    failure: dict[str, Any] | None = None,
    exit_summary: dict[str, Any] | None = None,
) -> None:
    """Writes the file only -- the caller's own commit picks up sessions.json (record_run_failure
    and exit_finalize_node already commit `.ai-dev-workflow`/their own path list; the reject path
    is the one exception and commits sessions.json itself, since its cleanup reset happens first
    and touches nothing else)."""
    sessions = await _read(provider, thread_id)
    normalized_failure = _build_failure(failure) if failure else None
    sessions = _close(
        sessions,
        thread_id=thread_id,
        run_id=run_id,
        status=status,
        ended_at=_now(),
        failure=normalized_failure,
        exit_summary=exit_summary,
    )
    await _write(provider, thread_id, sessions)


def _demo() -> None:
    """Self-check for the pure half: `uv run python -m src.session_index`."""
    # First session for a thread: appended, not upserted.
    sessions = _upsert([], thread_id="t1", run_id="r1", title="Add login", user="alice", target_branch="feature/login", started_at="2026-01-01T00:00:00Z")
    assert len(sessions) == 1 and sessions[0]["status"] == "in_progress" and sessions[0]["run_id"] == "r1"

    # A clarification round with a DIFFERENT non-empty title refreshes run_id/started_at in place
    # -- no second row.
    sessions = _upsert(sessions, thread_id="t1", run_id="r2", title="Add login with 2FA", user="alice", target_branch="feature/login", started_at="2026-01-01T00:05:00Z")
    assert len(sessions) == 1, sessions
    assert sessions[0]["run_id"] == "r2" and sessions[0]["title"] == "Add login with 2FA"

    # A round with an UNCHANGED title (or none) does NOT mint a new run_id/started_at.
    sessions = _upsert(sessions, thread_id="t1", run_id="r3", title="Add login with 2FA", user="alice", target_branch="feature/login", started_at="2026-01-01T00:10:00Z")
    assert len(sessions) == 1 and sessions[0]["run_id"] == "r2", sessions
    sessions = _upsert(sessions, thread_id="t1", run_id="r4", title="", user="alice", target_branch="feature/login", started_at="2026-01-01T00:11:00Z")
    assert sessions[0]["run_id"] == "r2", sessions

    # A second thread appends its own row, untouched by the first thread's upserts.
    sessions = _upsert(sessions, thread_id="t2", run_id="s1", title="Other repo work", user="bob", target_branch="main", started_at="2026-01-01T00:12:00Z")
    assert len(sessions) == 2, sessions

    # Stray duplicate in_progress rows for the SAME thread (e.g. legacy/bad data) all collapse to
    # one: the first is treated as primary, the rest are superseded.
    dup = [
        {"run_id": "d1", "thread_id": "t3", "title": "First", "user": "x", "target_branch": "main", "started_at": "t", "ended_at": None, "status": "in_progress", "failure": None, "exit": None},
        {"run_id": "d2", "thread_id": "t3", "title": "Second", "user": "x", "target_branch": "main", "started_at": "t", "ended_at": None, "status": "in_progress", "failure": None, "exit": None},
    ]
    result = _upsert(dup, thread_id="t3", run_id="d3", title="Third", user="x", target_branch="main", started_at="2026-01-01T00:13:00Z")
    assert [r["status"] for r in result] == ["in_progress", "superseded"], result
    assert result[0]["title"] == "Third" and result[0]["run_id"] == "d3"

    # _close: matches by (thread_id, run_id) first.
    closed = _close(sessions, thread_id="t1", run_id="r2", status="completed", ended_at="2026-01-01T01:00:00Z", exit_summary={"merge_ready": True, "pr_title": "Add 2FA login"})
    row = next(r for r in closed if r["thread_id"] == "t1")
    assert row["status"] == "completed" and row["ended_at"] == "2026-01-01T01:00:00Z"
    assert row["exit"] == {"merge_ready": True, "pr_title": "Add 2FA login"}

    # _close falls back to the thread's in_progress row when run_id doesn't match anything (e.g.
    # an escalate_node firing before intake ever recorded this run's own row). _close itself
    # stores `failure` verbatim -- normalizing raw payloads into {stage,type,message} is
    # end_session's job (via _build_failure), tested separately below.
    normalized = _build_failure({"stage": "plan", "type": "verification_cap_exceeded", "feedback": "  needs more detail  "})
    closed = _close(sessions, thread_id="t2", run_id="unknown-run", status="failed", ended_at="2026-01-01T01:00:00Z", failure=normalized)
    row = next(r for r in closed if r["thread_id"] == "t2")
    assert row["status"] == "failed"
    assert row["failure"] == {"stage": "plan", "type": "verification_cap_exceeded", "message": "needs more detail"}

    # _close is a no-op (not a raise) when nothing matches at all.
    closed = _close(sessions, thread_id="no-such-thread", run_id=None, status="failed", ended_at="2026-01-01T01:00:00Z")
    assert closed == sessions

    # failure.message prefers feedback over report, trims, and caps at 500 chars.
    built = _build_failure({"stage": "s", "type": "t", "feedback": "  short  ", "report": {"long": "x"}})
    assert built == {"stage": "s", "type": "t", "message": "short"}
    built = _build_failure({"stage": "s", "type": "t", "report": {"a": 1}})
    assert built["message"] == str({"a": 1})
    built = _build_failure({"stage": "s", "type": "t"})
    assert built["message"] == ""
    long_payload = _build_failure({"stage": "s", "type": "t", "feedback": "x" * 600})
    assert len(long_payload["message"]) == 500

    # _cap keeps the tail (recency order), leaves a short list alone.
    ten = [{"i": i} for i in range(10)]
    assert _cap(ten, 5) == ten[-5:]
    assert _cap(ten, 20) == ten

    # Eviction never drops an in_progress row, no matter how far from the tail it sits: a stalled
    # session near the front must stay reachable for a later end_session to close.
    stalled = {"run_id": "stalled", "thread_id": "t-stalled", "status": "in_progress"}
    closed_rows = [{"run_id": f"c{i}", "thread_id": f"t{i}", "status": "completed"} for i in range(5)]
    mixed = [stalled, *closed_rows]
    capped = _cap(mixed, 2)
    assert stalled in capped, capped
    assert [r["run_id"] for r in capped] == ["stalled", "c3", "c4"], capped

    print("session_index self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.session_index`
    _demo()
