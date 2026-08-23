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
    # Every container-destruction path routes through here (both providers' terminate(), the idle
    # reaper via terminate(), and DELETE /{thread_id}), so this is also the one place that can
    # reliably evict the Copilot/Claude sessions pointing INTO that container. Without it they
    # survive as live-looking handles to a destroyed sandbox -- the same phantom-state bug the
    # `registry.pop` comments in terminate() describe, one layer up. Imported here, not at module
    # scope: chat_model imports .sandbox (transitively, via whichever provider module it dispatches
    # to), so a top-level import cycles.
    #
    # forget_thread_sessions_everywhere(), not forget_thread_sessions(thread_id, provider=...): a
    # first attempt at this function tried to resolve a single `provider` live (via
    # get_provider()) and evict just that one -- reasoning that a session teardown has no
    # GraphState to read a pinned provider from, so live-resolving here was the same category
    # Ruling 2 already allows for session PROVISIONING. That reasoning missed that teardown isn't
    # actually symmetric with provisioning: provisioning needs to know which provider to build
    # a NEW session for, a question the live setting correctly answers; teardown just needs to
    # forget whatever ALREADY EXISTS, a question the live setting can get wrong. Reproduced
    # empirically: pin a thread to claude, flip the org setting to copilot, tear the thread down --
    # the live resolution evicted copilot's (empty) dict and left the thread's real claude session
    # ids untouched, which later surfaced as dead `--resume` tokens against the freshly recreated
    # container. forget_thread_sessions_everywhere() sidesteps the whole question by evicting both
    # (see its own docstring for why that's always safe, never just "usually fine") -- no DB call,
    # so this function is back to a plain sync `def` (it was only ever made async for the
    # `await get_provider()` this fix removes; every other line here was already synchronous).
    from ..chat_model import forget_thread_sessions_everywhere

    forget_thread_sessions_everywhere(thread_id)
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
