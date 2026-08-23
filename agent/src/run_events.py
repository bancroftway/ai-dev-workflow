"""Normalized event shape for Part 2 (run-visibility UI redesign) -- the durable counterpart to
`repo_files.append_ledger_entry`'s ephemeral JSON-lines ledger (a file inside the sandbox's own
workspace, gone once that sandbox is torn down). `run_event_store.py` persists these; this module
only defines the shape, so it has no DB import and nothing to self-check on its own -- exercised
end-to-end by run_event_store's `_demo()`.

Members/fields match the brief verbatim; `node` was added on top of it (see `run_event_store.py`'s
module docstring) because every real graph.py call site this task wires already distinguishes
draft/audit/verify within a stage, and that distinction is exactly what a run-visibility UI would
want to filter/group by -- losing it into a free-text `summary` would throw away real structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class RunEventType(StrEnum):
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    # Populated only when granularity allows (brief's own qualifier) -- no current call site emits
    # this; kept so a later task adding tool-call-level detail doesn't need a schema migration.
    TOOL_CALL = "tool_call"
    REASONING = "reasoning"
    GATE_PAUSED = "gate_paused"
    GATE_RESOLVED = "gate_resolved"


@dataclass(frozen=True)
class RunEvent:
    """One row of dbo.run_events. `seq`/`ts` are DB-assigned (IDENTITY + SYSUTCDATETIME default) --
    leave them unset when building an event to append; `run_event_store.append_event` returns a
    copy with both filled in from what the DB actually stored."""

    run_id: str
    session_id: str
    type: RunEventType
    stage: str | None = None
    node: str | None = None
    summary: str | None = None
    payload: dict[str, Any] | None = None
    token_usage: dict[str, Any] | None = None
    seq: int | None = None
    ts: datetime | None = None
