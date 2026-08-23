"""Provider dispatch for chat models: Claude vs Copilot, chosen by whoever calls in, not once at
process start.

Until Task 3, PROVIDER was computed ONCE from AGENT_PROVIDER at module-import time, and every name
below (get_chat_model_for_thread, close_session, ...) was bound then too, directly to whichever
provider module happened to be active at that moment. That binding is permanent for the life of
the process -- and this codebase's real processes are long-lived: sessions_api.py's uvicorn server
handles many sessions over hours/days, run_headless.py's own run can span a full pipeline. Once an
org admin uses the Settings UI (org_settings.py/Task 1) to change the active provider in the DB, an
import-time-bound name -- including every OTHER module's own `from .chat_model import
get_chat_model_for_thread`, captured at ITS OWN import time -- would keep calling the OLD provider
forever, with no way to notice the change short of restarting every worker process. get_provider()
below is the fix for THAT bug: it re-resolves the org's saved setting on a short TTL rather than
once at import time.

Ruling 4 (docs/superpowers/plans/part-4-org-settings-tasks.md), added after Task 3's first pass
got this next part wrong: the 7 functions below that actually DO the dispatching
(get_chat_model_for_thread, close_session, close_thread_session, forget_thread_sessions,
get_session_id, read_skill_invocations, secret_env_names) do NOT call get_provider() themselves.
Each takes the provider to dispatch to as a **required,
keyword-only `provider` parameter** instead, with no default. The first version of this file had
each of these 7 resolve the live setting internally on every call -- which looked like the same
"per-call, not per-process" fix as get_provider() itself, but actually defeated the entire point of
GraphState.provider (Task 4): a run pins its provider ONCE, at intake, specifically so a live
setting change never splits one run across two providers' sessions/CLIs mid-flight. If these 7
functions each independently re-read the shared 30-second-TTL cache, then any one of them, called
more than ~30 seconds after the cache was last warmed (the ordinary case for a run spanning minutes
to hours), would resolve to whatever the org setting says RIGHT NOW rather than what the run is
pinned to -- silently undoing Task 4's pinning for the very call sites it exists to protect. Worse
for the session-lifecycle functions specifically (close_session, get_session_id,
forget_thread_sessions): a wrong-provider resolution doesn't just build the wrong model, it
operates on the WRONG PROVIDER'S own session-tracking dict for a thread actually running under the
other one -- the same resource-leak/wrong-dispatch shape Part 1's Tasks 10-11 already had to hunt
down once (there, a hardcoded value; here, a live-drifting one). So: the caller resolves the
provider exactly once (a graph node reads its own pinned `state["provider"]`; genuine
provisioning-time code with no state yet calls get_provider() itself) and hands it to whichever of
these 7 it needs -- nothing in this module quietly re-derives it on their behalf.

get_provider() itself is unaffected by Ruling 4 -- it still exists, still does the live,
TTL-cached DB read, and is still exactly what intake_node (GraphState.provider, once per run) and
genuine provisioning-time code (sessions_api.py, run_headless.py's startup, the sandbox
provisioning lazy imports) call to get that one live value in the first place. It just no longer
gets called from inside the other 7 -- those are told, not asked.

forget_thread_sessions_everywhere() below is a THIRD category, discovered chasing Ruling 4's own
"never live-resolve" logic to its actual conclusion at teardown: sandbox/registry.py's pop() and
the sandbox provision() reprovision branches (local_docker.py, azure_aci.py) all evict a dying
thread's cached session ids, and the first attempt at fixing them (per the required-provider
pattern above) had each resolve `provider` live and call plain forget_thread_sessions(thread_id,
provider=provider) -- which reintroduced Ruling 4's exact bug at a new location: pin a thread to
claude, flip the org setting to copilot, tear the thread down, and the live resolution evicts
COPILOT's (empty) dict while the thread's real claude session ids survive, later becoming dead
`--resume` tokens against a freshly recreated container. The actual fix is not "resolve the RIGHT
provider" -- at teardown there is no right provider left to resolve, since the sandbox is already
gone and neither provider's cached session is resumable either way. The answer is "evict from
both"; a thread only ever has real entries under the one provider it actually ran under, so
clearing the other one is a guaranteed no-op, never a correctness risk.

sandbox/factory.py's get_sandbox_provider() looks superficially similar (env var -> module
selection) but is solving a different problem: which SandboxProvider backend to run against (local
Docker vs Azure Container Instances) is an infra choice, fixed for a deployment's whole life, so
memoizing it forever in a module-level singleton after its first call is correct there. The point
of THIS module is the opposite -- an admin must be able to change the active provider without a
redeploy -- so a permanent memoized value would just be the same bug under a different name; only a
short TTL cache (re-resolved periodically, never forever) fits that requirement.

get_runtime_auth_token() below is a related but separate fix: sessions_api.py and run_headless.py
used to read ANTHROPIC_API_KEY/GITHUB_TOKEN from the process environment directly, by hand, via
their own `chat_model.PROVIDER == "claude"` ternary -- so an admin's Settings-UI-saved credential
(org_credential_vault.py/Task 2) would never reach a real sandboxed session. Defined here because
it is new chat_model.py surface, not a call-site conversion.

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

    Split out of get_provider() (its only caller) so the cache-hit check itself stays trivially
    testable/readable on its own.
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
    hits the DB. Per Ruling 4, the 7 dispatch functions below no longer call this themselves --
    only intake_node (to populate GraphState.provider once per run) and genuine provisioning-time
    code with no state yet (sessions_api.py, run_headless.py's startup, sandbox provisioning) call
    this directly; everything else is handed the resolved value as an explicit `provider` argument.
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
    provider: str,
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
    whichever provider the caller says (module docstring, Ruling 4) rather than a name bound at
    import time OR resolved fresh in here.

    `provider` is required, keyword-only, no default (Ruling 4): a graph node passes its own
    pinned `state["provider"]`; nothing else legitimately calls this. This function used to resolve
    it itself internally -- removed, since a call late in a long-running graph run could then
    silently pick up a live setting change instead of the run's own pinned value, defeating
    GraphState.provider's whole purpose.

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


async def close_thread_session(thread_id: str, *, provider: str) -> None:
    """Evict every cached session for a thread (call on graph run completion/error).

    `provider` is required, keyword-only, no default (Ruling 4) -- the caller's own pinned
    `state["provider"]`, not resolved in here; see chat_model.py's module docstring for why an
    internal live re-resolution would silently defeat GraphState.provider's per-run pinning.

    Async in both providers only for call-site parity -- neither actually awaits anything
    internally, session eviction is a pure dict pop either way (see each module's own docstring).
    """
    await _provider_module(provider).close_thread_session(thread_id)


def forget_thread_sessions(thread_id: str, *, provider: str) -> None:
    """Drop cached session ids for a thread whose sandbox is already gone, for the ONE provider
    the caller names.

    `provider` is required, keyword-only, no default (Ruling 4) -- the caller's own pinned
    `state["provider"]`, not resolved in here. Sync in both providers (a pure dict-key-prefix pop,
    no I/O). NOT what teardown code should call -- see forget_thread_sessions_everywhere() just
    below for why a single dying-sandbox eviction has no correct single `provider` to resolve at
    all, live or otherwise.
    """
    _provider_module(provider).forget_thread_sessions(thread_id)


def forget_thread_sessions_everywhere(thread_id: str) -> None:
    """Teardown eviction: drop this thread's cached session ids under BOTH providers.

    The only real callers are sandbox/registry.py's pop() and the sandbox provision() reprovision
    branches (local_docker.py, azure_aci.py) -- all three destroy the sandbox a cached session id
    would have pointed into, so nothing under EITHER provider is resumable afterward regardless of
    which one this thread actually ran under. There is no live setting to resolve here, unlike
    every other function in this module: `provider` (this module's usual required argument) names
    which provider a session should be built for or looked up under going forward, a question that
    only makes sense while the sandbox it would run in still exists. A first attempt at this
    function tried to resolve `provider` anyway (live, via get_provider(), following the same
    pattern as forget_thread_sessions above) -- reproduced empirically as a real bug: pin a thread
    to claude, flip the org setting to copilot, tear the thread down, and the live resolution
    evicts copilot's (empty) dict while the thread's real claude session ids survive, later
    becoming dead `--resume` tokens the moment the container is recreated with a fresh $HOME. That
    is Ruling 4's exact failure shape relocated to teardown, not a different bug -- the fix is not
    "resolve the right provider," it is recognizing there IS no single right provider to resolve at
    a moment when neither one's cached session is usable anyway. Evicting both is provably safe: a
    given thread can only ever have real entries in ONE provider's `_session_ids` dict within a
    single process (whichever one it actually dispatched to), so the other provider's call here is
    always a no-op pop-of-nothing.
    """
    for module in (claude_chat_model, copilot_chat_model):
        module.forget_thread_sessions(thread_id)


async def close_session(thread_id: str, stage: str, role: str, *, provider: str) -> None:
    """Drop one (thread, stage, role) session so the next call starts fresh -- see each provider
    module's own close_session docstring for why a fresh session, not a retry in the same one, is
    what actually recovers from a stage whose session history contains a fabricated claim.

    `provider` is required, keyword-only, no default (Ruling 4) -- the caller's own pinned
    `state["provider"]`, not resolved in here. Dispatches to whichever provider the caller says --
    the actual fix for a caller that imported this name once, at process start (module docstring);
    resolving the provider inside this function instead of accepting it would have reintroduced a
    different staleness bug (Ruling 4), not fixed the original one.
    """
    await _provider_module(provider).close_session(thread_id, stage, role)


def get_session_id(thread_id: str, stage: str, role: str, *, provider: str) -> str | None:
    """The session id backing one (thread, stage, role), or None if none was created yet -- lets a
    gate (gates/skill_gate.py) verify what a stage's session actually did, rather than trusting the
    model's self-report.

    `provider` is required, keyword-only, no default (Ruling 4) -- the specific thread's own pinned
    provider, threaded in from its caller. Sync in both providers (a dict lookup).
    """
    return _provider_module(provider).get_session_id(thread_id, stage, role)


async def read_skill_invocations(
    provider: SandboxProvider, thread_id: str, session_id: str, *, active_provider: str
) -> list[str] | None:
    """Skill names a session actually invoked, read from its own transcript, or None if
    unverifiable -- see each provider module's own docstring for its fail-open contract (an
    infrastructure gap must never masquerade as "no skills were invoked").

    `provider` here is the pre-existing SandboxProvider connection-object parameter (the
    exec_in_sandbox target) -- kept under its original name so a keyword-calling caller
    (`read_skill_invocations(provider=...)`) is unaffected by this rewrite. It is unrelated to this
    module's own "claude"/"copilot" provider string, which is why THAT one is named
    `active_provider` here rather than colliding with the existing `provider` parameter -- required,
    keyword-only, no default (Ruling 4): the specific thread's own pinned provider, threaded in from
    its caller, not resolved in here.
    """
    return await _provider_module(active_provider).read_skill_invocations(provider, thread_id, session_id)


def secret_env_names(*, provider: str) -> set[str]:
    """Provider-specific env var names -- see each provider module's own docstring, since despite
    the shared name this means two DIFFERENT things per provider (Claude: what the sandbox must
    already have set for the CLI to authenticate; Copilot: what to redact from a turn's own shell
    output). A reader who only ever looks at one provider's version should not assume the other
    works the same way; this dispatcher does not paper over that, it just forwards to whichever
    provider the caller says.

    `provider` is required, keyword-only, no default (Ruling 4) -- not resolved in here.
    """
    return _provider_module(provider).secret_env_names()


__all__ = [
    "get_provider",
    "get_runtime_auth_token",
    "get_chat_model_for_thread",
    "close_thread_session",
    "forget_thread_sessions",
    "forget_thread_sessions_everywhere",
    "close_session",
    "get_session_id",
    "read_skill_invocations",
    "secret_env_names",
    "ainvoke_structured",
]


def _demo() -> None:
    """Self-check: proves get_provider()'s TTL cache + env-var fallback (still forced/flipped via
    _force_provider() below, since those two are the only remaining live-resolution surface in
    this module), and proves each of the 7 dispatch functions sends its call to the module its
    caller's explicit `provider` argument names (Ruling 4) -- passed directly per call now, not
    forced through the env var/cache the way it was pre-Ruling-4, since none of these 7 reads that
    cache anymore. Section 2 got SIMPLER for exactly that reason: no _force_provider() dance, just
    two calls with two different literal `provider=` values and a check each landed on the right
    module. Section 2 also proves forget_thread_sessions_everywhere() clears both providers with
    no `provider` argument at all, and that the 7 functions' "required, no default" contract itself
    actually holds (a `TypeError` on the missing-argument call, not a silent fallback).

    Offline only, matching org_settings.py's and org_credential_vault.py's own self-check
    limitation on this branch: org_settings.get_org_settings and
    org_credential_vault.get_org_credential are monkeypatched throughout (no live DB/vault in this
    environment); _provider_cache is force-cleared between get_provider()/get_runtime_auth_token()
    flips as a stand-in for real TTL expiry, which is time-based and would otherwise make a
    same-process demo wait out the full 30 seconds between every assertion.
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

        # === 2. Per-call dispatch proof for every one of the 7 functions that now take a
        # required, keyword-only `provider` argument (Ruling 4) -- passed directly below, no
        # _force_provider()/cache interaction, since none of these 7 reads the shared cache
        # anymore (that is precisely the point of Ruling 4's correction). ===

        # get_chat_model_for_thread (sync): each provider's model class reports a distinct
        # _llm_type ("claude-code" vs "github-copilot") -- a real signal already on the class, no
        # seeding required. Called with both literal values, then "claude" again, to rule out a
        # one-way fluke (e.g. a stale default only visible on the second call).
        assert get_chat_model_for_thread(thread_id, stage, role, provider="claude")._llm_type == "claude-code"
        assert get_chat_model_for_thread(thread_id, stage, role, provider="copilot")._llm_type == "github-copilot"
        assert get_chat_model_for_thread(thread_id, stage, role, provider="claude")._llm_type == "claude-code", "flip back to claude failed"

        # get_session_id / forget_thread_sessions (sync): seed each provider's OWN _session_ids
        # dict with a distinguishing marker under the same key; confirm the right one comes back.
        claude_chat_model._session_ids[key] = "claude-marker"
        copilot_chat_model._session_ids[key] = "copilot-marker"
        assert get_session_id(thread_id, stage, role, provider="claude") == "claude-marker"
        assert get_session_id(thread_id, stage, role, provider="copilot") == "copilot-marker"

        forget_thread_sessions(thread_id, provider="claude")
        assert key not in claude_chat_model._session_ids, "forget_thread_sessions did not reach claude_chat_model"
        assert copilot_chat_model._session_ids.get(key) == "copilot-marker", "forget_thread_sessions touched the wrong provider"
        copilot_chat_model._session_ids.pop(key, None)

        # close_session / close_thread_session (async): same idea, dispatched through asyncio.run.
        claude_chat_model._session_ids[key] = "claude-marker"
        copilot_chat_model._session_ids[key] = "copilot-marker"
        asyncio.run(close_session(thread_id, stage, role, provider="claude"))
        assert key not in claude_chat_model._session_ids, "close_session did not reach claude_chat_model"
        assert copilot_chat_model._session_ids.get(key) == "copilot-marker", "close_session touched the wrong provider"
        asyncio.run(close_thread_session(thread_id, provider="copilot"))
        assert key not in copilot_chat_model._session_ids, "close_thread_session did not reach copilot_chat_model"

        # secret_env_names (sync): each provider returns a different literal set (see each
        # module's own docstring for why the shared name means two different things per provider).
        assert secret_env_names(provider="claude") == {"ANTHROPIC_API_KEY"}
        assert secret_env_names(provider="copilot") == {
            "COPILOT_SDK_AUTH_TOKEN",
            "COPILOT_CONNECTION_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GITHUB_TOKEN",
        }

        # read_skill_invocations (async): Copilot's real implementation always returns None
        # unconditionally (per its own docstring); Claude's real implementation parses a fake
        # sandbox's transcript output and returns a real list. A minimal duck-typed fake stands in
        # for SandboxProvider -- read_skill_invocations only ever calls .exec_in_sandbox on it, so
        # nothing else needs implementing. `active_provider=` here is this module's own
        # "claude"/"copilot" dispatch argument -- not to be confused with the positional
        # `fake_provider` (the SandboxProvider connection-object stand-in), the exact collision
        # this function's own docstring explains it avoids by using two different names.
        class _FakeSandboxResult:
            ok = True
            stdout = '{"type": "assistant", "message": {"content": []}}\n'
            stderr = ""

        class _FakeSandboxProvider:
            async def exec_in_sandbox(self, thread_id: str, command: str):
                return _FakeSandboxResult()

        fake_provider = _FakeSandboxProvider()
        assert asyncio.run(read_skill_invocations(fake_provider, thread_id, "sess-1", active_provider="copilot")) is None
        assert asyncio.run(read_skill_invocations(fake_provider, thread_id, "sess-1", active_provider="claude")) == [], (
            "claude_chat_model.read_skill_invocations should have parsed the fake transcript"
        )

        # forget_thread_sessions_everywhere (sync): the teardown-only eighth function, deliberately
        # NOT one of the 7 that take a `provider` argument -- it has none, by design (module
        # docstring: there is no single right provider to resolve at teardown, so it evicts both).
        # Seeds both providers' _session_ids under the same key and confirms one call clears both.
        claude_chat_model._session_ids[key] = "claude-marker"
        copilot_chat_model._session_ids[key] = "copilot-marker"
        forget_thread_sessions_everywhere(thread_id)
        assert key not in claude_chat_model._session_ids, "forget_thread_sessions_everywhere missed claude_chat_model"
        assert key not in copilot_chat_model._session_ids, "forget_thread_sessions_everywhere missed copilot_chat_model"

        # Ruling 4's "required, no default" contract itself -- not just that the 7 dispatch
        # perform correctly when given a provider, but that omitting it fails loudly rather than
        # silently. Locks this in against a later "just add provider=None for convenience" edit
        # quietly reopening the exact mid-run staleness bug Ruling 4 exists to close.
        # get_session_id stands in for all 7 (the contract -- keyword-only, no default -- is
        # identical across them; this is not testing get_session_id's own logic again).
        try:
            get_session_id(thread_id, stage, role)  # type: ignore[call-arg]
        except TypeError:
            pass
        else:
            raise AssertionError(
                "get_session_id must require `provider` with no default -- a default would let a "
                "forgetful caller silently fall back to a live-resolved value, reopening Ruling 4's "
                "mid-run staleness bug"
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

    print(
        "chat_model dispatch self-check: all assertions passed (per-call dispatch proven for all "
        "7 required-provider functions, forget_thread_sessions_everywhere's both-provider evict, "
        "and the required-argument contract itself)"
    )


if __name__ == "__main__":
    # Re-dispatch through the PACKAGE name on purpose. `python -m src.chat_model` loads this file
    # as "__main__", so a direct _demo() call would import this module a second time as a
    # non-package import -- splitting this module's own module-level `_provider_cache` tuple across
    # two sys.modules entries and silently invalidating the cache-hit assertions in _demo() above.
    # Same convention as claude_chat_model.py, copilot_chat_model.py, org_settings.py.
    from src.chat_model import _demo as _packaged_demo

    _packaged_demo()
