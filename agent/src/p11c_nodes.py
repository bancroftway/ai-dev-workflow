"""P11c -- dependency upgrades: the one P11 sub-stage needing a genuine verify-or-loop-back
decision between draft and audit, so it doesn't fit StageSpec's draft->audit->verify->gate
template (verify happens BEFORE audit here, not after) or RebuildSpec (this owns real package
upgrades, not just a build-fix).

Chain: p11c_pre (deterministic: list outdated deps) -> p11c_draft (write access: upgrade +
regenerate lockfiles) -> p11c_verify (deterministic: clean rebuild + tests) -> route:
  pass -> p11c_audit (read-only risk review) -> falls through
  fail + cycles remain -> loop to p11c_draft with failure evidence
  fail + cap hit -> p11c_revert (deterministic git revert) -> p11c_notice_gate (informational
    human gate -- does NOT block the rest of P11, dependency freshness being valuable-but-optional,
    not correctness-critical the way coverage/dup/license are)

Verification status: NOT exercised against a real sandbox, same caveat as P8/P10/rebuild.py.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from . import git_ops, model_config, repo_files
from .copilot_chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

P11C_MAX_CYCLES = int(os.environ.get("P11C_MAX_CYCLES", "2"))


class P11cState(TypedDict):
    cycle_count: int
    pre_upgrade_commit: str | None
    last_verify_ok: bool
    last_output_tail: str
    audit_notes: str


def default_p11c_state() -> P11cState:
    return {"cycle_count": 0, "pre_upgrade_commit": None, "last_verify_ok": False, "last_output_tail": "", "audit_notes": ""}


def _resolve_outdated_command(tech_stack: dict[str, Any]) -> str | None:
    if tech_stack.get("dotnet_detected"):
        return "dotnet list package --outdated 2>&1"
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if "typescript" in languages or "javascript" in languages:
        return "npx --yes npm-check-updates 2>&1"
    return None


def _resolve_build_test_command(tech_stack: dict[str, Any]) -> str | None:
    if tech_stack.get("dotnet_detected"):
        return "dotnet build -warnaserror && dotnet test 2>&1"
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if "typescript" in languages or "javascript" in languages:
        return "npm install && npm run build --if-present && npx --yes vitest run 2>&1"
    return None


async def p11c_pre_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p11c = dict(state.get("p11c") or default_p11c_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p11c": p11c}
    provider = get_sandbox_provider()
    if p11c["pre_upgrade_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        p11c["pre_upgrade_commit"] = head.stdout.strip() if head.ok else None
    return {"p11c": p11c}


async def p11c_draft_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p11c = dict(state.get("p11c") or default_p11c_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p11c": p11c}

    tech_stack = ((state.get("stages") or {}).get("tech-stack") or {}).get("approved_content") or {}
    outdated_command = _resolve_outdated_command(tech_stack)
    outdated_report = ""
    if outdated_command:
        result = await get_sandbox_provider().exec_in_sandbox(thread_id, outdated_command)
        outdated_report = (result.stdout or result.stderr or "")[-4000:]

    model = get_chat_model_for_thread(
        thread_id,
        "plan",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("plan", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        agent_mode="autopilot",
    )
    feedback = f"\n\nThe previous upgrade attempt broke the build/tests:\n{p11c['last_output_tail']}" if not p11c["last_verify_ok"] and p11c["cycle_count"] > 0 else ""
    prompt = (
        "Upgrade this repo's outdated dependencies and regenerate lockfiles. Prefer the latest "
        "compatible minor/patch versions; only take a major-version bump if the outdated report "
        f"shows no viable alternative. Outdated dependency report:\n\n{outdated_report}{feedback}"
    )
    await model.ainvoke([SystemMessage(content="You are the Dependency Upgrade Agent."), HumanMessage(content=prompt)])
    await repo_files.append_ledger_entry(get_sandbox_provider(), thread_id, {"stage": "p11c", "node": "draft", "token_usage": model._last_usage})
    return {"p11c": p11c}


async def p11c_verify_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p11c = dict(state.get("p11c") or default_p11c_state())
    if sandbox_registry.get(thread_id) is None:
        p11c["last_verify_ok"] = True
        return {"p11c": p11c}

    provider = get_sandbox_provider()
    tech_stack = ((state.get("stages") or {}).get("tech-stack") or {}).get("approved_content") or {}
    command = _resolve_build_test_command(tech_stack)
    if command is None:
        p11c["last_verify_ok"] = True
        return {"p11c": p11c}

    result = await provider.exec_in_sandbox(thread_id, command)
    p11c["last_verify_ok"] = result.ok
    p11c["last_output_tail"] = (result.stdout or result.stderr or "")[-4000:]
    if not result.ok:
        p11c["cycle_count"] = p11c["cycle_count"] + 1
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p11c", "node": "verify", "ok": result.ok, "cycle": p11c["cycle_count"]})
    return {"p11c": p11c}


def make_p11c_route_after_verify():
    def route(state: dict[str, Any]) -> str:
        p11c = state.get("p11c") or default_p11c_state()
        if p11c["last_verify_ok"]:
            return "audit"
        if p11c["cycle_count"] < P11C_MAX_CYCLES:
            return "retry"
        return "revert"

    return route


async def p11c_audit_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Read-only risk review of the (already build/test-verified) upgrade -- names no skill in
    particular; this is a plain judgment pass, not an adversarial audit like P11a's."""
    thread_id = config["configurable"]["thread_id"]
    p11c = dict(state.get("p11c") or default_p11c_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p11c": p11c}

    model = get_chat_model_for_thread(
        thread_id,
        "p11c-upgrade",
        "audit",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("p11c-upgrade", "audit"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=["builtin:view", "builtin:grep", "builtin:glob", "builtin:task_complete", "builtin:ask_user"],
    )
    response = await model.ainvoke(
        [
            SystemMessage(content="You are the Dependency Upgrade Risk Reviewer."),
            HumanMessage(content="Review the dependency changes just made (git diff on lockfiles/manifests) for any concerning major-version jump or unusual transitive change. Summarize risk in a few sentences."),
        ]
    )
    p11c["audit_notes"] = getattr(response, "content", str(response))
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p11c", "node": "audit", "token_usage": model._last_usage})
    return {"p11c": p11c}


async def p11c_revert_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p11c = dict(state.get("p11c") or default_p11c_state())
    if sandbox_registry.get(thread_id) is not None and p11c["pre_upgrade_commit"] is not None:
        provider = get_sandbox_provider()
        await provider.exec_in_sandbox(thread_id, f"git checkout {p11c['pre_upgrade_commit']} -- . && git checkout {p11c['pre_upgrade_commit']}")
        await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p11c", "node": "revert", "to": p11c["pre_upgrade_commit"]})
    return {"p11c": p11c}


async def p11c_notice_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Informational human gate -- does NOT block the rest of P11 (dependency freshness is
    valuable-but-optional, not correctness-critical the way coverage/dup/license are). The graph
    proceeds to P11d regardless of how this interrupt resolves; it exists purely so a human sees
    that the upgrade attempt was reverted, not to gate further progress."""
    p11c = state.get("p11c") or default_p11c_state()
    interrupt({"stage": "p11c", "type": "dependency_upgrade_reverted", "last_output_tail": p11c["last_output_tail"]})
    return {}
