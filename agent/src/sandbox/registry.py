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


async def pop(thread_id: str) -> SandboxSession | None:
    # Every container-destruction path routes through here (both providers' terminate(), the idle
    # reaper via terminate(), and DELETE /{thread_id}), so this is also the one place that can
    # reliably evict the Copilot sessions pointing INTO that container. Without it they survive as
    # live-looking handles to a destroyed sandbox -- the same phantom-state bug the `registry.pop`
    # comments in terminate() describe, one layer up. Imported here, not at module scope:
    # chat_model imports .sandbox (transitively, via whichever provider module it dispatches to),
    # so a top-level import cycles.
    #
    # forget_thread_sessions now requires an explicit `provider` (Ruling 4,
    # docs/superpowers/plans/part-4-org-settings-tasks.md) -- traced every real call path into this
    # function (both providers' terminate(), which is ALSO the idle reaper's own teardown path, plus
    # sessions_api.py's two DELETE-style endpoints) and none of them ever has a GraphState to read
    # a pinned provider from: a session teardown is not a graph-node execution, it can fire from a
    # timer with no run in progress at all. This is genuinely the same category Ruling 2 already
    # carves out for session PROVISIONING (no state yet, read the live setting) -- destroying a
    # session is the mirror-image infrastructure operation, not a mid-run LLM dispatch, so this
    # function was made async (all 4 real callers already awaited an async function at this call
    # site, so this cost nothing) specifically so it can resolve the live setting itself via
    # get_provider() rather than fabricate a "pinned" value that does not exist anywhere in this
    # call chain. Worst case on a provider that changed since this session's own sessions were
    # created: this evicts the (now-)wrong provider's _session_ids entry and leaves the real one
    # stale -- a harmless cleanup miss, never a wrong dispatch or a crash (chat_model.py module
    # docstring's own framing of what this function protects).
    from ..chat_model import forget_thread_sessions, get_provider

    forget_thread_sessions(thread_id, provider=await get_provider())
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
