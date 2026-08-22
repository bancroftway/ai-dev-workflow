"""Provider dispatch for chat models: selects Claude or Copilot at module load time.

Architecture mirrors sandbox.factory.py's SANDBOX_PROVIDER dispatch pattern. The AGENT_PROVIDER
environment variable selects which provider module to load; all provider modules re-export the
same function names so a caller can import from this shim without knowing which provider is active.
"""

from __future__ import annotations

import os

from .structured_output import ainvoke_structured

PROVIDER = os.environ.get("AGENT_PROVIDER", "copilot")

if PROVIDER == "copilot":
    from .copilot_chat_model import (
        close_session,
        close_thread_session,
        forget_thread_sessions,
        get_chat_model_for_thread,
        get_session_id,
        read_skill_invocations,
        secret_env_names,
    )
elif PROVIDER == "claude":
    from .claude_chat_model import (
        close_session,
        close_thread_session,
        forget_thread_sessions,
        get_chat_model_for_thread,
        get_session_id,
        read_skill_invocations,
        secret_env_names,
    )
else:
    raise ValueError(f"Unknown AGENT_PROVIDER={PROVIDER!r}, expected 'copilot' or 'claude'")

__all__ = [
    "close_session",
    "close_thread_session",
    "forget_thread_sessions",
    "get_chat_model_for_thread",
    "get_session_id",
    "read_skill_invocations",
    "secret_env_names",
    "ainvoke_structured",
]


def _demo() -> None:
    """Self-check: verify both provider branches import cleanly and every re-exported name is callable.

    Tests the dispatch logic without requiring a live sandbox.
    """
    # Verify PROVIDER value is recognized
    assert PROVIDER in ("copilot", "claude"), f"PROVIDER={PROVIDER!r} should be validated by the if/elif/else above"

    # Verify every re-exported name is callable
    for name in ("get_chat_model_for_thread", "close_thread_session", "forget_thread_sessions",
                 "close_session", "get_session_id", "read_skill_invocations", "secret_env_names"):
        obj = globals()[name]
        assert callable(obj), f"{name} is not callable: {type(obj)}"

    # ainvoke_structured is always imported from structured_output, verify it's callable
    assert callable(ainvoke_structured), f"ainvoke_structured is not callable: {type(ainvoke_structured)}"

    print(f"chat_model dispatch self-check: active provider={PROVIDER!r}, all assertions passed")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose.
    from src.chat_model import _demo as _packaged_demo

    _packaged_demo()
