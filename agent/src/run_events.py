"""Normalized event shape for Part 2 (run-visibility UI redesign) -- the durable counterpart to
`repo_files.append_ledger_entry`'s ephemeral JSON-lines ledger (a file inside the sandbox's own
workspace, gone once that sandbox is torn down). `run_event_store.py` persists these; this module
defines the shape (still no DB import) plus `encode_io_text` (Workstream 3), a pure string/bytes
helper with its own tiny self-check below -- exercised end-to-end otherwise by run_event_store's
`_demo()`.

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

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage


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
    # Overview-tab redraft history (Workstream 3): the full prompt sent to the model and its full
    # response, for a draft/audit/fix NODE_FINISHED event -- UTF-8 encoded before storage (VARBINARY,
    # not NVARCHAR, halves stored bytes). input_size/output_size are the byte lengths, kept alongside
    # so the Overview tab can render sizes without fetching the (potentially large) text itself.
    input_text: bytes | None = None
    output_text: bytes | None = None
    input_size: int | None = None
    output_size: int | None = None


def encode_io_text(messages: list[BaseMessage], output: str) -> tuple[bytes, bytes, int, int]:
    """Overview-tab redraft history (Workstream 3): joins a draft/audit/fix call's full prompt
    into one role-labeled, human-readable blob (SYSTEM:/HUMAN: -- the redraft loop's prompts are
    always these two, never AIMessage; see graph.py's _build_plan_prompt-style functions, which
    rebuild the whole prompt fresh from state every lap rather than replaying prior turns) and
    UTF-8-encodes both halves for dbo.run_events' VARBINARY columns. Returns
    (input_bytes, output_bytes, input_size, output_size) -- the two sizes are just len() of the
    two byte strings, computed once here so every call site doesn't repeat it.
    """
    labeled = [f"{'SYSTEM' if isinstance(m, SystemMessage) else 'HUMAN'}:\n{m.content}" for m in messages]
    input_bytes = "\n\n".join(labeled).encode("utf-8")
    output_bytes = output.encode("utf-8")
    return input_bytes, output_bytes, len(input_bytes), len(output_bytes)


def _demo() -> None:
    """Self-check: `cd agent && uv run python -m src.run_events` (no DB, no sandbox needed)."""
    input_bytes, output_bytes, input_size, output_size = encode_io_text(
        [SystemMessage(content="be helpful"), HumanMessage(content="draft the tests")],
        "Added 3 test files.",
    )
    assert input_bytes == "SYSTEM:\nbe helpful\n\nHUMAN:\ndraft the tests".encode("utf-8"), input_bytes
    assert output_bytes == "Added 3 test files.".encode("utf-8"), output_bytes
    assert input_size == len(input_bytes) and output_size == len(output_bytes), (input_size, output_size)

    # Non-ASCII content must UTF-8-encode (not crash, not silently mangle) -- sizes are BYTE
    # lengths, not character counts, so a multi-byte character must inflate input_size accordingly.
    unicode_input, _, unicode_size, _ = encode_io_text([SystemMessage(content="café")], "ok")
    assert unicode_input == "SYSTEM:\ncafé".encode("utf-8"), unicode_input
    assert unicode_size == len("SYSTEM:\ncafé".encode("utf-8")) > len("SYSTEM:\ncafe"), (
        "a multi-byte character must count as more than one byte", unicode_size,
    )

    print("run_events self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.run_events
    _demo()
