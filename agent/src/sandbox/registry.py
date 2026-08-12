"""In-memory thread_id -> SandboxSession registry.

Mirrors the InMemorySaver()/InMemoryStore() choice already made for GraphState in graph.py --
consistent with the architecture plan's Decision 4 (small internal tool, don't over-engineer).
This is process-local: a restart of the agent forgets which sandboxes were provisioned, same
caveat as the existing in-memory checkpointer/store, and the same thing that needs revisiting if
this ever moves off "one small persistent agent process" (see the plan's Section D scaling note).
"""

from __future__ import annotations

from .provider import SandboxSession

_sessions: dict[str, SandboxSession] = {}


def get(thread_id: str) -> SandboxSession | None:
    return _sessions.get(thread_id)


def set(thread_id: str, session: SandboxSession) -> None:
    _sessions[thread_id] = session


def pop(thread_id: str) -> SandboxSession | None:
    return _sessions.pop(thread_id, None)
