"""In-memory thread_id -> SandboxSession registry.

Mirrors the InMemorySaver()/InMemoryStore() choice already made for GraphState in graph.py --
consistent with the architecture plan's Decision 4 (small internal tool, don't over-engineer).
This is process-local: a restart of the agent forgets which sandboxes were provisioned, same
caveat as the existing in-memory checkpointer/store, and the same thing that needs revisiting if
this ever moves off "one small persistent agent process" (see the plan's Section D scaling note).
"""

from __future__ import annotations

from typing import Any

from .provider import SandboxSession

_sessions: dict[str, SandboxSession] = {}

# One-shot signals a node needs but that graph state doesn't carry on its own (registry meta, not
# GraphState, since it must be visible to graph.py's intake before the thread has ever hydrated
# any state) -- just `resume` today. Durable provision-time facts (user_login, source_branch,
# work_branch) live in session_store.py (SQL) instead, not here -- they used to live in this dict
# and were lost on every agent restart, which is exactly the caveat below warns about.
_meta: dict[str, dict[str, Any]] = {}


def get(thread_id: str) -> SandboxSession | None:
    return _sessions.get(thread_id)


def set(thread_id: str, session: SandboxSession) -> None:
    _sessions[thread_id] = session


def pop(thread_id: str) -> SandboxSession | None:
    _meta.pop(thread_id, None)
    return _sessions.pop(thread_id, None)


def set_meta(thread_id: str, **kwargs: Any) -> None:
    _meta.setdefault(thread_id, {}).update(kwargs)


def get_meta(thread_id: str) -> dict[str, Any]:
    return _meta.get(thread_id, {})


def pop_meta_flag(thread_id: str, key: str) -> bool:
    """Removes and returns a boolean-ish meta flag, defaulting to False when absent -- for
    one-shot signals like `resume` that must be consumed by the first intake that sees them and
    never apply again to a later, unrelated run on the same thread."""
    return bool(_meta.get(thread_id, {}).pop(key, False))
