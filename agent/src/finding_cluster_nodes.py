"""finding-cluster -- dependency upgrades: the one audit-cluster sub-stage needing a genuine verify-or-loop-back
decision between draft and audit, so it doesn't fit StageSpec's draft->audit->verify->gate
template (verify happens BEFORE audit here, not after) or RebuildSpec (this owns real package
upgrades, not just a build-fix).

Chain: finding_cluster_pre (deterministic: list outdated deps) -> finding_cluster_draft (write access: upgrade +
regenerate lockfiles) -> finding_cluster_verify (deterministic: clean rebuild + tests) -> route:
  pass -> finding_cluster_audit (read-only risk review) -> falls through
  fail + cycles remain -> loop to finding_cluster_draft with failure evidence
  fail + cap hit -> finding_cluster_revert (deterministic git revert) -> finding_cluster_notice_gate (informational
    human gate -- does NOT block the rest of audit-cluster, dependency freshness being valuable-but-optional,
    not correctness-critical the way coverage/dup/license are)

Verification status: NOT exercised against a real sandbox, same caveat as quality-remediation/security-remediation/rebuild.py.
"""

from __future__ import annotations

import logging
import os
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

from . import config as workflow_config
from . import git_ops, model_config, repo_files, tech_stack_signals
from .copilot_chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

FINDING_CLUSTER_MAX_CYCLES = int(os.environ.get("FINDING_CLUSTER_MAX_CYCLES", "2"))


class FindingClusterState(TypedDict):
    cycle_count: int
    pre_upgrade_commit: str | None
    last_verify_ok: bool
    last_output_tail: str
    audit_notes: str


def default_finding_cluster_state() -> FindingClusterState:
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
        # One `cd` prefix covers both commands -- it's a single shell invocation, so the cwd it
        # sets persists across the whole `&&` chain.
        return f"{tech_stack_signals.dotnet_root_prefix(tech_stack)}dotnet build -warnaserror && dotnet test 2>&1"
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if "typescript" in languages or "javascript" in languages:
        return "npm install && npm run build --if-present && npx --yes vitest run 2>&1"
    return None


async def finding_cluster_pre_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    finding_cluster = dict(state.get("finding_cluster") or default_finding_cluster_state())
    if sandbox_registry.get(thread_id) is None:
        return {"finding_cluster": finding_cluster}
    provider = get_sandbox_provider()
    if finding_cluster["pre_upgrade_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        finding_cluster["pre_upgrade_commit"] = head.stdout.strip() if head.ok else None
    return {"finding_cluster": finding_cluster}


async def finding_cluster_draft_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    finding_cluster = dict(state.get("finding_cluster") or default_finding_cluster_state())
    if sandbox_registry.get(thread_id) is None:
        return {"finding_cluster": finding_cluster}

    tech_stack = ((state.get("stages") or {}).get("tech-stack") or {}).get("approved_content") or {}
    outdated_command = _resolve_outdated_command(tech_stack)
    outdated_report = ""
    if outdated_command:
        result = await get_sandbox_provider().exec_in_sandbox(thread_id, outdated_command)
        outdated_report = (result.stdout or result.stderr or "")[-4000:]

    # Own session key (finding-cluster-upgrade:draft), not plan:draft. Sharing plan's key returned plan's
    # cached read-only session (tools lock at session creation) so this autopilot upgrade agent
    # silently couldn't write, and it inherited plan's whole conversation. Uses its own dedicated
    # models.yaml entry too, which was previously dead config.
    model = get_chat_model_for_thread(
        thread_id,
        "finding-cluster-upgrade",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("finding-cluster-upgrade", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        agent_mode="autopilot",
    )
    feedback = f"\n\nThe previous upgrade attempt broke the build/tests:\n{finding_cluster['last_output_tail']}" if not finding_cluster["last_verify_ok"] and finding_cluster["cycle_count"] > 0 else ""
    system, template = load_prompt_pair("finding_cluster_dependency_upgrade")
    prompt = render_prompt(template, outdated_report=outdated_report, feedback=feedback)
    await model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
    await repo_files.append_ledger_entry(get_sandbox_provider(), thread_id, {"stage": "finding_cluster", "node": "draft", "token_usage": model._last_usage})
    return {"finding_cluster": finding_cluster}


async def finding_cluster_verify_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    finding_cluster = dict(state.get("finding_cluster") or default_finding_cluster_state())
    if sandbox_registry.get(thread_id) is None:
        finding_cluster["last_verify_ok"] = True
        return {"finding_cluster": finding_cluster}

    provider = get_sandbox_provider()
    tech_stack = ((state.get("stages") or {}).get("tech-stack") or {}).get("approved_content") or {}
    command = _resolve_build_test_command(tech_stack)
    if command is None:
        finding_cluster["last_verify_ok"] = True
        return {"finding_cluster": finding_cluster}

    result = await provider.exec_in_sandbox(thread_id, command)
    finding_cluster["last_verify_ok"] = result.ok
    finding_cluster["last_output_tail"] = (result.stdout or result.stderr or "")[-4000:]
    if not result.ok:
        finding_cluster["cycle_count"] = finding_cluster["cycle_count"] + 1
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "finding_cluster", "node": "verify", "ok": result.ok, "cycle": finding_cluster["cycle_count"]})
    if result.ok:
        # Verified dependency upgrade: commit manifests/lockfiles (and anything else the upgrade
        # touched) so the revert path has a clean pre/post boundary and the work branch gets it.
        await git_ops.commit_all(provider, thread_id, "ai-dev-workflow: finding-cluster dependency upgrades")
    return {"finding_cluster": finding_cluster}


def make_finding_cluster_route_after_verify():
    def route(state: dict[str, Any]) -> str:
        finding_cluster = state.get("finding_cluster") or default_finding_cluster_state()
        if finding_cluster["last_verify_ok"]:
            return "audit"
        if finding_cluster["cycle_count"] < FINDING_CLUSTER_MAX_CYCLES:
            return "retry"
        return "revert"

    return route


async def finding_cluster_audit_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Read-only risk review of the (already build/test-verified) upgrade -- names no skill in
    particular; this is a plain judgment pass, not an adversarial audit like adversarial-audit's."""
    thread_id = config["configurable"]["thread_id"]
    finding_cluster = dict(state.get("finding_cluster") or default_finding_cluster_state())
    if sandbox_registry.get(thread_id) is None:
        return {"finding_cluster": finding_cluster}

    model = get_chat_model_for_thread(
        thread_id,
        "finding-cluster-upgrade",
        "audit",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("finding-cluster-upgrade", "audit"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    review_system, review_human = load_prompt_pair("finding_cluster_risk_review")
    response = await model.ainvoke(
        [SystemMessage(content=review_system), HumanMessage(content=review_human)]
    )
    finding_cluster["audit_notes"] = getattr(response, "content", str(response))
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "finding_cluster", "node": "audit", "token_usage": model._last_usage})
    return {"finding_cluster": finding_cluster}


async def finding_cluster_revert_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    finding_cluster = dict(state.get("finding_cluster") or default_finding_cluster_state())
    if sandbox_registry.get(thread_id) is not None and finding_cluster["pre_upgrade_commit"] is not None:
        provider = get_sandbox_provider()
        # `git checkout <sha>` would detach HEAD, orphaning every subsequent commit (license-audit, metrics-report,
        # exit) so the run's output dies with the container. reset --hard rewinds tracked files to
        # the pre-upgrade commit while keeping HEAD on the branch.
        # ponytail: a brand-new lockfile the upgrade *created* (repo had none before) is untracked
        # and survives this; add `git clean` scoped to lockfiles if that ever bites.
        await provider.exec_in_sandbox(thread_id, f"git reset --hard {finding_cluster['pre_upgrade_commit']}")
        await repo_files.append_ledger_entry(provider, thread_id, {"stage": "finding_cluster", "node": "revert", "to": finding_cluster["pre_upgrade_commit"]})
    return {"finding_cluster": finding_cluster}


async def finding_cluster_notice_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Informational notice -- previously an interrupt, now just a log line. Dependency freshness
    is valuable-but-optional, the graph proceeds to license-audit regardless, and the revert node
    above already wrote the durable ledger entry; pausing a whole run to show a human a
    non-blocking notice was the only cost. The two human checkpoints are specification and plan."""
    finding_cluster = state.get("finding_cluster") or default_finding_cluster_state()
    logger.warning(
        "finding-cluster dependency upgrade was reverted; last output tail: %s",
        (finding_cluster.get("last_output_tail") or "")[-500:],
    )
    return {}
