"""Provider dispatch for chat models: resolves Claude vs Copilot on EVERY call, not once at
process start.

Until this task, PROVIDER was computed ONCE from AGENT_PROVIDER at module-import time, and every
name below (get_chat_model_for_thread, close_session, ...) was bound then too, directly to
whichever provider module happened to be active at that moment. That binding is permanent for the
life of the process -- and this codebase's real processes are long-lived: sessions_api.py's uvicorn
server handles many sessions over hours/days, run_headless.py's own run can span a full pipeline.
Once an org admin uses the Settings UI (a later task, built on org_settings.py/Task 1) to change
the active provider in the DB, an import-time-bound name -- including every OTHER module's own
`from .chat_model import get_chat_model_for_thread`, captured at ITS OWN import time -- would keep
calling the OLD provider forever, with no way to notice the change short of restarting every worker
process. That is the real bug this file fixes, not a hypothetical: get_provider() below re-resolves
the org's saved setting on a short TTL, and every previously-re-exported name is now a real
function that calls it (or a sync-safe equivalent, for the handful that are themselves sync) on
every invocation -- so a caller that imported one of these names once, at ITS OWN module-load time,
still tracks the live setting rather than a snapshot from whenever it happened to import this file.

sandbox/factory.py's get_sandbox_provider() looks superficially similar (env var -> module
selection) but is solving a different problem: which SandboxProvider backend to run against (local
Docker vs Azure Container Instances) is an infra choice, fixed for a deployment's whole life, so
memoizing it forever in a module-level singleton after its first call is correct there. The point
of THIS module is the opposite -- an admin must be able to change the active provider without a
redeploy -- so a permanent memoized value would just be the same bug under a different name; only a
short TTL cache (re-resolved periodically, never forever) fits that requirement.

get_runtime_auth_token() below is a related but separate fix: sessions_api.py and run_headless.py
currently read ANTHROPIC_API_KEY/GITHUB_TOKEN from the process environment directly, by hand, via
their own `chat_model.PROVIDER == "claude"` ternary -- so an admin's Settings-UI-saved credential
(org_credential_vault.py/Task 2) would never reach a real sandboxed session even after this task
ships. Defined here because it is new chat_model.py surface, not a call-site conversion.

ainvoke_structured (from structured_output.py) is unaffected by any of this -- it was already
provider-agnostic before this task and stays a plain re-export.
"""

from __future__ import annotations

import asyncio
import os
import time
import types
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

# Both provider modules are imported unconditionally now (previously chat_model.py imported only
# whichever one PROVIDER named, at process start). Checked before this rewrite that this is safe:
# neither claude_chat_model.py nor copilot_chat_model.py does any I/O, network call, subprocess, or
# thread spawn at module scope -- each one's top level only builds a few plain dicts/sets and
# defines its own BaseChatModel subclass (a pydantic model class, no instances constructed at
# import time) -- and both already import the exact same internal dependencies (config, telemetry,
# cli_agent_exec, sandbox) regardless of which provider was previously active, so those imports are
# already-paid costs, not new ones. The only genuinely new cost from importing "the other" module
# is that one class/function-definition pass, the same cost any ordinary Python import pays.
from . import claude_chat_model, copilot_chat_model, org_credential_vault, org_settings
from .sandbox import SandboxProvider, SandboxSession
from .structured_output import ainvoke_structured

# 30 seconds. Every real caller below sits inside (or just before) a sandboxed CLI-exec turn that
# itself routinely takes many seconds to minutes (config.CLI_AGENT_TURN_TIMEOUT_SECONDS and this
# pipeline's other multi-second-to-minute stage timeouts) -- a 30s cache window is imperceptible
# against that latency, so it costs nothing in practice while still bounding
# org_settings.get_org_settings() to at most one DB round trip per 30 wall-clock seconds no matter
# how many dispatch calls happen inside that window. It also satisfies the other half of the
# fallback-default requirement from the caller's side: an org admin who flips the Settings UI sees
# the whole fleet converge on the new provider within half a minute -- "soon, not instantly" (this
# task's own brief), not "next deploy."
_PROVIDER_CACHE_TTL_SECONDS = 30

# (resolved provider value, time.monotonic() at fetch) -- a plain module-level tuple, not a
# decorator/cache library this codebase doesn't already depend on. None until the first resolution.
_provider_cache: tuple[str, float] | None = None


def _provider_module(provider: str) -> types.ModuleType:
    """The module implementing `provider`'s dispatch surface.

    The one validation choke-point every dispatching function below routes through, so an
    unrecognized value (a typo'd AGENT_PROVIDER, a corrupt org_settings row) fails loud here --
    the same fail-fast contract the old import-time if/elif/else enforced, just deferred from
    "at process start" to "at first actual dispatch."
    """
    if provider == "claude":
        return claude_chat_model
    if provider == "copilot":
        return copilot_chat_model
    raise ValueError(f"Unknown provider {provider!r}, expected 'copilot' or 'claude'")


def _cached_provider_if_fresh() -> str | None:
    """Sync, non-blocking read of the shared TTL cache -- None on a cold or expired cache.

    Shared by get_provider() (the authoritative async path, which repopulates it from the DB) and
    _get_provider_sync() (the sync fallback below), so a value either one fetched benefits the
    other for the rest of its TTL window.
    """
    if _provider_cache is None:
        return None
    value, fetched_at = _provider_cache
    if time.monotonic() - fetched_at >= _PROVIDER_CACHE_TTL_SECONDS:
        return None
    return value


async def get_provider() -> str:
    """The org's active provider ("claude" or "copilot"), resolved fresh at most once per
    _PROVIDER_CACHE_TTL_SECONDS.

    Reads org_settings.get_org_settings() on a cold/expired cache; falls back to
    os.environ.get("AGENT_PROVIDER", "copilot") when that returns None (a fresh deployment whose
    admin hasn't visited the Settings UI yet -- org_settings.get_org_settings's own docstring notes
    this is exactly where that fallback belongs). This is the ONLY place in this module that ever
    hits the DB; every dispatching function below calls this (or _get_provider_sync(), for the
    handful that are themselves sync) instead of re-implementing the cache/fallback logic itself.
    """
    global _provider_cache
    cached = _cached_provider_if_fresh()
    if cached is not None:
        return cached

    settings = await org_settings.get_org_settings()
    value = settings.provider if settings is not None else os.environ.get("AGENT_PROVIDER", "copilot")
    _provider_module(value)  # fail fast on an unrecognized value, before caching it
    _provider_cache = (value, time.monotonic())
    return value


def _get_provider_sync() -> str:
    """Sync-safe stand-in for get_provider(), used only by the re-exported functions that are
    themselves sync (get_chat_model_for_thread, forget_thread_sessions, get_session_id,
    secret_env_names -- all four are plain `def`s in BOTH provider modules, doing no I/O) and
    therefore cannot `await get_provider()`. Any of these can be called from inside code that is
    itself already running an event loop (e.g. a LangGraph async node calling
    get_chat_model_for_thread without awaiting it, since it isn't a coroutine function) --
    `asyncio.run()` in that situation raises RuntimeError("cannot be called from a running event
    loop"), so there is no safe way to fall through to a real DB read from here.

    Reads the same shared cache get_provider() populates -- a sync caller benefits from whatever an
    async caller already fetched, within the same TTL window -- but on a cold/expired cache it
    never itself hits the DB, and never writes _provider_cache either; it only ever falls back
    straight to the env var, the same default get_provider() itself falls back to. Net effect: the
    staleness bound here is NOT a fixed _PROVIDER_CACHE_TTL_SECONDS -- it is self-correcting rather
    than time-bounded. A sync-only call path can lag an admin's DB-saved provider change for as
    long as it takes until the NEXT time any async-dispatched function in this module
    (get_provider(), close_session(), close_thread_session(), read_skill_invocations(), or
    get_runtime_auth_token()) actually runs and warms the shared cache -- if none of those ever
    run in a given process, this function never once consults the DB, no matter how much time
    passes. In practice that window is normally short (a real graph run's own lifecycle calls
    close_session/close_thread_session, and Task 5 is expected to wire get_runtime_auth_token()
    early in session provisioning), but that is a fact about this codebase's call patterns, not a
    guarantee this function itself enforces. This is an explicit trade-off, not an oversight -- the
    alternative is blocking a sync function on network I/O it cannot safely perform, or risking the
    RuntimeError above.
    """
    cached = _cached_provider_if_fresh()
    if cached is not None:
        return cached
    value = os.environ.get("AGENT_PROVIDER", "copilot")
    _provider_module(value)  # same fail-fast validation as get_provider()
    return value


async def get_runtime_auth_token() -> str:
    """The credential a sandboxed turn needs to authenticate as the CURRENTLY active provider's
    CLI: the org's saved vault credential if an admin has configured one, else the same
    provider-keyed env var get_provider()'s own fallback already implies (ANTHROPIC_API_KEY for
    claude, GITHUB_TOKEN for copilot).

    Fixes a real gap: sessions_api.py and run_headless.py each hand-roll this exact
    `"claude" -> ANTHROPIC_API_KEY else GITHUB_TOKEN` choice today by reading chat_model.PROVIDER
    and the env directly, so an admin's UI-saved credential (org_credential_vault.py, Task 2) would
    never reach a real session even once those call sites stop reading the now-removed PROVIDER
    constant -- Task 5's job is to point them at this function instead of their own copy of the
    logic.
    """
    provider = await get_provider()
    settings = await org_settings.get_org_settings()
    if settings is not None and settings.credential_secret_name is not None:
        return await org_credential_vault.get_org_credential(settings.credential_secret_name)

    if provider == "claude":
        return os.environ.get("ANTHROPIC_API_KEY", "")
    return os.environ.get("GITHUB_TOKEN", "")


def get_chat_model_for_thread(
    thread_id: str,
    stage: str,
    role: str,
    *,
    github_token: str | None = None,
    model_name: str | None = None,
    sandbox: SandboxSession | None = None,
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] = "plan",
    available_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
    pre_tool_use_hook: Any | None = None,
    mcp_servers: dict[str, Any] | None = None,
    custom_agents: list[dict] | None = None,
    agent: str | None = None,
    tools: list[Any] | None = None,
    disabled_skills: list[str] | None = None,
    response_schema: type[BaseModel] | None = None,
) -> BaseChatModel:
    """Build the chat model for one LangGraph thread's (stage, role) session, dispatching to
    whichever provider is active FOR THIS CALL (module docstring) rather than a name bound at
    import time.

    Sync, not async: both claude_chat_model.get_chat_model_for_thread and
    copilot_chat_model.get_chat_model_for_thread are themselves plain `def`s (they only construct a
    pydantic model, no I/O), so this uses _get_provider_sync() rather than `await get_provider()`
    -- see that helper's own docstring for the event-loop hazard that rules out the alternative.

    github_token and response_schema are each accepted by only ONE provider's real function
    (github_token: copilot_chat_model only; response_schema: claude_chat_model only -- see each
    module's own get_chat_model_for_thread docstring for why). Every current call site
    unconditionally passes github_token=os.environ.get("GITHUB_TOKEN") regardless of which provider
    is actually active (confirmed across every call site: graph.py x3, e2e_nodes.py, metrics_nodes.
    py, preflight_nodes.py x2, rebuild.py, test_hardening_nodes.py x2; stack_runner.py is the one
    exception that omits it) -- accepting it here and simply not forwarding it on the claude branch
    keeps that unconditional pattern working under either provider, rather than raising
    TypeError("unexpected keyword argument") the moment AGENT_PROVIDER=claude actually runs, which
    is what calling the OLD import-time-bound alias directly would have done.
    """
    provider = _get_provider_sync()
    common: dict[str, Any] = dict(
        model_name=model_name,
        sandbox=sandbox,
        agent_mode=agent_mode,
        available_tools=available_tools,
        excluded_tools=excluded_tools,
        pre_tool_use_hook=pre_tool_use_hook,
        mcp_servers=mcp_servers,
        custom_agents=custom_agents,
        agent=agent,
        tools=tools,
        disabled_skills=disabled_skills,
    )
    if provider == "claude":
        return claude_chat_model.get_chat_model_for_thread(
            thread_id, stage, role, response_schema=response_schema, **common
        )
    if provider == "copilot":
        return copilot_chat_model.get_chat_model_for_thread(
            thread_id, stage, role, github_token=github_token, **common
        )
    raise ValueError(f"Unknown provider {provider!r}, expected 'copilot' or 'claude'")


async def close_thread_session(thread_id: str) -> None:
    """Evict every cached session for a thread (call on graph run completion/error).

    Async in both providers only for call-site parity -- neither actually awaits anything
    internally, session eviction is a pure dict pop either way (see each module's own docstring).
    Dispatches per-call, same as every function in this module.
    """
    provider = await get_provider()
    await _provider_module(provider).close_thread_session(thread_id)


def forget_thread_sessions(thread_id: str) -> None:
    """Drop cached session ids for a thread whose sandbox is already gone.

    Sync in both providers (a pure dict-key-prefix pop, no I/O) -- uses _get_provider_sync() rather
    than `await get_provider()` for the same event-loop-safety reason get_chat_model_for_thread
    does. sandbox.registry.pop() already routes through this name for Copilot today; per-call
    dispatch here is what lets the same call site also reach Claude sessions once registry.py is
    updated to call this module instead of copilot_chat_model directly (Task 5's job, not this
    module's -- see this task's report).
    """
    provider = _get_provider_sync()
    _provider_module(provider).forget_thread_sessions(thread_id)


async def close_session(thread_id: str, stage: str, role: str) -> None:
    """Drop one (thread, stage, role) session so the next call starts fresh -- see each provider
    module's own close_session docstring for why a fresh session, not a retry in the same one, is
    what actually recovers from a stage whose session history contains a fabricated claim.
    Dispatches to whichever provider is active for THIS call, the actual fix for a caller that
    imported this name once, at process start (module docstring).
    """
    provider = await get_provider()
    await _provider_module(provider).close_session(thread_id, stage, role)


def get_session_id(thread_id: str, stage: str, role: str) -> str | None:
    """The session id backing one (thread, stage, role), or None if none was created yet -- lets a
    gate (gates/skill_gate.py) verify what a stage's session actually did, rather than trusting the
    model's self-report.

    Sync in both providers (a dict lookup); see _get_provider_sync()'s docstring for why this can't
    `await get_provider()`.
    """
    provider = _get_provider_sync()
    return _provider_module(provider).get_session_id(thread_id, stage, role)


async def read_skill_invocations(provider: SandboxProvider, thread_id: str, session_id: str) -> list[str] | None:
    """Skill names a session actually invoked, read from its own transcript, or None if
    unverifiable -- see each provider module's own docstring for its fail-open contract (an
    infrastructure gap must never masquerade as "no skills were invoked").

    `provider` here is the pre-existing SandboxProvider connection-object parameter (the
    exec_in_sandbox target) -- kept under its original name so a keyword-calling caller
    (`read_skill_invocations(provider=...)`) is unaffected by this rewrite. It is unrelated to this
    module's own "claude"/"copilot" provider string, resolved separately below as `active_provider`
    to avoid the naming collision.
    """
    active_provider = await get_provider()
    return await _provider_module(active_provider).read_skill_invocations(provider, thread_id, session_id)


def secret_env_names() -> set[str]:
    """Provider-specific env var names -- see each provider module's own docstring, since despite
    the shared name this means two DIFFERENT things per provider (Claude: what the sandbox must
    already have set for the CLI to authenticate; Copilot: what to redact from a turn's own shell
    output). A reader who only ever looks at one provider's version should not assume the other
    works the same way; this dispatcher does not paper over that, it just forwards to whichever
    provider is active.

    Sync in both providers; see _get_provider_sync()'s docstring for why this can't
    `await get_provider()`.
    """
    provider = _get_provider_sync()
    return _provider_module(provider).secret_env_names()


__all__ = [
    "get_provider",
    "get_runtime_auth_token",
    "get_chat_model_for_thread",
    "close_thread_session",
    "forget_thread_sessions",
    "close_session",
    "get_session_id",
    "read_skill_invocations",
    "secret_env_names",
    "ainvoke_structured",
]


def _demo() -> None:
    """Self-check: proves get_provider()'s TTL cache + env-var fallback, and proves every
    re-exported name dispatches PER CALL rather than once (the actual bug this task fixes -- see
    module docstring), by forcing the resolved provider to flip between calls in the same process
    and confirming each function follows it to the correct underlying module. A stale binding
    captured once at import time (the old design) could never do this -- only a function that
    re-resolves the provider on every call can.

    Offline only, matching org_settings.py's and org_credential_vault.py's own self-check
    limitation on this branch: org_settings.get_org_settings and
    org_credential_vault.get_org_credential are monkeypatched throughout (no live DB/vault in this
    environment); _provider_cache is force-cleared between flips as a stand-in for real TTL expiry,
    which is time-based and would otherwise make a same-process demo wait out the full 30 seconds
    between every assertion.
    """
    from datetime import datetime

    global _provider_cache

    def _force_provider(name: str) -> None:
        """Pin AGENT_PROVIDER and invalidate the shared cache -- stands in for TTL expiry so this
        demo can flip the active provider between calls without a real 30-second wait."""
        global _provider_cache
        os.environ["AGENT_PROVIDER"] = name
        _provider_cache = None

    thread_id, stage, role = "demo-thread", "demo-stage", "demo-role"
    key = f"{thread_id}:{stage}:{role}"

    # Save every piece of process-global state this self-check temporarily overrides.
    original_get_org_settings = org_settings.get_org_settings
    original_get_org_credential = org_credential_vault.get_org_credential
    original_agent_provider_env = os.environ.get("AGENT_PROVIDER")
    original_anthropic_env = os.environ.get("ANTHROPIC_API_KEY")
    original_github_env = os.environ.get("GITHUB_TOKEN")

    try:
        # === 1. get_provider(): env-var fallback when org_settings has no row (fresh deployment),
        # and proof the TTL cache actually caches (does not re-fetch inside its own window). ===
        stub_calls = {"n": 0}

        async def _stub_no_settings():
            stub_calls["n"] += 1
            return None

        org_settings.get_org_settings = _stub_no_settings
        _force_provider("claude")
        resolved = asyncio.run(get_provider())
        assert resolved == "claude", f"expected env-var fallback 'claude', got {resolved!r}"
        assert stub_calls["n"] == 1, "get_provider() should have consulted org_settings on a cold cache"

        resolved_again = asyncio.run(get_provider())
        assert resolved_again == "claude", f"cached call changed the resolved value: {resolved_again!r}"
        assert stub_calls["n"] == 1, "get_provider() re-fetched instead of serving the TTL cache"

        # === 2. Per-call dispatch proof for every re-exported name. ===
        org_settings.get_org_settings = _stub_no_settings  # keep the env-var path active throughout

        # get_chat_model_for_thread (sync): each provider's model class reports a distinct
        # _llm_type ("claude-code" vs "github-copilot") -- a real signal already on the class, no
        # seeding required. Flipped twice, in both directions, to rule out a one-way fluke.
        _force_provider("claude")
        assert get_chat_model_for_thread(thread_id, stage, role)._llm_type == "claude-code"
        _force_provider("copilot")
        assert get_chat_model_for_thread(thread_id, stage, role)._llm_type == "github-copilot"
        _force_provider("claude")
        assert get_chat_model_for_thread(thread_id, stage, role)._llm_type == "claude-code", "flip back to claude failed"

        # get_session_id / forget_thread_sessions (sync): seed each provider's OWN _session_ids
        # dict with a distinguishing marker under the same key; confirm the right one comes back.
        claude_chat_model._session_ids[key] = "claude-marker"
        copilot_chat_model._session_ids[key] = "copilot-marker"
        _force_provider("claude")
        assert get_session_id(thread_id, stage, role) == "claude-marker"
        _force_provider("copilot")
        assert get_session_id(thread_id, stage, role) == "copilot-marker"

        _force_provider("claude")
        forget_thread_sessions(thread_id)
        assert key not in claude_chat_model._session_ids, "forget_thread_sessions did not reach claude_chat_model"
        assert copilot_chat_model._session_ids.get(key) == "copilot-marker", "forget_thread_sessions touched the wrong provider"
        copilot_chat_model._session_ids.pop(key, None)

        # close_session / close_thread_session (async): same idea, dispatched through asyncio.run.
        claude_chat_model._session_ids[key] = "claude-marker"
        copilot_chat_model._session_ids[key] = "copilot-marker"
        _force_provider("claude")
        asyncio.run(close_session(thread_id, stage, role))
        assert key not in claude_chat_model._session_ids, "close_session did not reach claude_chat_model"
        assert copilot_chat_model._session_ids.get(key) == "copilot-marker", "close_session touched the wrong provider"
        _force_provider("copilot")
        asyncio.run(close_thread_session(thread_id))
        assert key not in copilot_chat_model._session_ids, "close_thread_session did not reach copilot_chat_model"

        # secret_env_names (sync): each provider returns a different literal set (see each
        # module's own docstring for why the shared name means two different things per provider).
        _force_provider("claude")
        assert secret_env_names() == {"ANTHROPIC_API_KEY"}
        _force_provider("copilot")
        assert secret_env_names() == {
            "COPILOT_SDK_AUTH_TOKEN",
            "COPILOT_CONNECTION_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GITHUB_TOKEN",
        }

        # read_skill_invocations (async): Copilot's real implementation always returns None
        # unconditionally (per its own docstring); Claude's real implementation parses a fake
        # sandbox's transcript output and returns a real list. A minimal duck-typed fake stands in
        # for SandboxProvider -- read_skill_invocations only ever calls .exec_in_sandbox on it, so
        # nothing else needs implementing.
        class _FakeSandboxResult:
            ok = True
            stdout = '{"type": "assistant", "message": {"content": []}}\n'
            stderr = ""

        class _FakeSandboxProvider:
            async def exec_in_sandbox(self, thread_id: str, command: str):
                return _FakeSandboxResult()

        fake_provider = _FakeSandboxProvider()
        _force_provider("copilot")
        assert asyncio.run(read_skill_invocations(fake_provider, thread_id, "sess-1")) is None
        _force_provider("claude")
        assert asyncio.run(read_skill_invocations(fake_provider, thread_id, "sess-1")) == [], (
            "claude_chat_model.read_skill_invocations should have parsed the fake transcript"
        )

        # === 3. get_runtime_auth_token(): vault path + env-var fallback path. ===
        async def _stub_settings_with_secret():
            return org_settings.OrgSettings(
                provider="claude",
                credential_secret_name="org-provider-credential",
                updated_at=datetime(2026, 8, 21, 12, 0, 0),
                updated_by="admin",
            )

        async def _stub_get_org_credential(secret_name: str) -> str:
            assert secret_name == "org-provider-credential", secret_name
            return "vault-secret-value"

        org_settings.get_org_settings = _stub_settings_with_secret
        org_credential_vault.get_org_credential = _stub_get_org_credential
        _force_provider("claude")
        assert asyncio.run(get_provider()) == "claude"
        token = asyncio.run(get_runtime_auth_token())
        assert token == "vault-secret-value", f"expected the vault-fetched credential, got {token!r}"

        # Claude + no configured secret: must fall back to ANTHROPIC_API_KEY, not the vault and
        # not the other provider's env var. Symmetric with the copilot case below -- both no-secret
        # branches of get_runtime_auth_token() need their own proof, since they are two separate
        # `if`/`return` lines, not one shared code path a single case could stand in for.
        async def _stub_settings_claude_no_secret():
            return org_settings.OrgSettings(
                provider="claude",
                credential_secret_name=None,
                updated_at=datetime(2026, 8, 21, 12, 0, 0),
                updated_by="admin",
            )

        org_settings.get_org_settings = _stub_settings_claude_no_secret
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-test-token"
        _force_provider("claude")
        token = asyncio.run(get_runtime_auth_token())
        assert token == "anthropic-test-token", f"expected ANTHROPIC_API_KEY fallback, got {token!r}"

        async def _stub_settings_no_secret():
            return org_settings.OrgSettings(
                provider="copilot",
                credential_secret_name=None,
                updated_at=datetime(2026, 8, 21, 12, 0, 0),
                updated_by="admin",
            )

        org_settings.get_org_settings = _stub_settings_no_secret
        os.environ["GITHUB_TOKEN"] = "gh-test-token"
        _force_provider("copilot")
        token = asyncio.run(get_runtime_auth_token())
        assert token == "gh-test-token", f"expected GITHUB_TOKEN fallback, got {token!r}"

        # === 4. Unknown provider value fails loud, same as the old import-time if/elif/else. ===
        try:
            _provider_module("bogus")
        except ValueError:
            pass
        else:
            raise AssertionError("_provider_module should reject an unrecognized provider value")

    finally:
        org_settings.get_org_settings = original_get_org_settings
        org_credential_vault.get_org_credential = original_get_org_credential
        claude_chat_model._session_ids.pop(key, None)
        copilot_chat_model._session_ids.pop(key, None)
        for name, original in (
            ("AGENT_PROVIDER", original_agent_provider_env),
            ("ANTHROPIC_API_KEY", original_anthropic_env),
            ("GITHUB_TOKEN", original_github_env),
        ):
            if original is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = original
        _provider_cache = None

    print("chat_model dispatch self-check: all assertions passed (per-call dispatch proven for all 7 re-exported names)")


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.chat_model` loads this file
    # as "__main__", so a direct _demo() call would import this module a second time as a
    # non-package import -- splitting this module's own module-level `_provider_cache` tuple across
    # two sys.modules entries and silently invalidating the cache-hit assertions in _demo() above.
    # Same convention as claude_chat_model.py, copilot_chat_model.py, org_settings.py.
    from src.chat_model import _demo as _packaged_demo

    _packaged_demo()
