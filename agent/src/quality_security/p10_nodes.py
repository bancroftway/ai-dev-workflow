"""P10 -- code security: same bespoke-cluster shape as P8 (agent/src/quality_security/p8_nodes.py),
parameterized differently. Chain: p10_scan -> p10_triage -> p10_ledger_write -> p10_fix -> R(p10)
-> p10_gate_check -> (loop to p10_scan | p10_human_gate).

Verification status, stated plainly, same caveat as P8: NOT exercised against a real sandbox.
Semgrep/Trivy/gitleaks are not installed by agent/sandbox-image/Dockerfile today -- all three
would need adding (Semgrep and gitleaks are both pip/binary installs; Trivy is a binary release
download, similar to how the Dockerfile already fetches the Copilot CLI binary) before this can
run for real. Severity normalization is a real simplification from the plan's stated intent -- see
severity.py's own docstring.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from .. import git_ops, model_config, repo_files
from ..copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from ..sandbox import registry as sandbox_registry
from ..sandbox.factory import get_sandbox_provider
from .sarif import Finding, make_finding_key, parse_sarif
from .schemas import TriageResponse
from .severity import GITLEAKS_SEVERITY, SEMGREP_SEVERITY_MAP, TRIVY_SEVERITY_MAP, meets_or_exceeds
from .suppressions import append_suppression, check_no_silent_suppression

P10_MAX_CYCLES = int(os.environ.get("P10_MAX_CYCLES", "3"))
# The strict gate: zero unsuppressed findings of this severity or above (raised from
# high/critical-only, per the plan's explicit "Stricter security gate" decision). INFO-tier
# findings remain advisory-only, never blocking.
P10_SEVERITY_FLOOR = os.environ.get("P10_SEVERITY_FLOOR", "low")

SEMGREP_SARIF_PATH = "agent-work/semgrep.sarif"
TRIVY_SARIF_PATH = "agent-work/trivy.sarif"
TRIVY_SBOM_PATH = ".ai-dev-workflow/sbom.cyclonedx.json"
GITLEAKS_REPORT_PATH = "agent-work/gitleaks.json"


class P10State(TypedDict):
    cycle_count: int
    findings: list[dict[str, Any]]
    decisions: dict[str, dict[str, Any]]
    baseline_commit: str | None
    sbom_ok: bool
    last_gate_report: dict[str, Any] | None


def default_p10_state() -> P10State:
    return {
        "cycle_count": 0,
        "findings": [],
        "decisions": {},
        "baseline_commit": None,
        "sbom_ok": False,
        "last_gate_report": None,
    }


def _parse_gitleaks(raw_json: str) -> list[Finding]:
    try:
        entries = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    findings: list[Finding] = []
    for entry in entries:
        rule_id = entry.get("RuleID", "gitleaks-secret")
        file_path = entry.get("File", "unknown")
        findings.append(
            Finding(
                finding_key=make_finding_key("gitleaks", rule_id, file_path),
                tool="gitleaks",
                rule_id=rule_id,
                severity=GITLEAKS_SEVERITY,
                raw_severity=GITLEAKS_SEVERITY,
                file=file_path,
                line=entry.get("StartLine"),
                message=entry.get("Description", "Potential secret detected"),
            )
        )
    return findings


async def p10_scan_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p10 = dict(state.get("p10") or default_p10_state())

    if sandbox_registry.get(thread_id) is None:
        return {"p10": p10}

    provider = get_sandbox_provider()
    if p10["baseline_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        p10["baseline_commit"] = head.stdout.strip() if head.ok else None

    findings: list[Finding] = []

    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work .ai-dev-workflow")
    await provider.exec_in_sandbox(
        thread_id, f"semgrep scan --config auto --config p/security-audit --sarif --output {SEMGREP_SARIF_PATH} 2>&1"
    )
    raw_semgrep = await repo_files.read_repo_file(provider, thread_id, SEMGREP_SARIF_PATH)
    if raw_semgrep is not None:
        findings.extend(parse_sarif(raw_semgrep, SEMGREP_SEVERITY_MAP))

    await provider.exec_in_sandbox(
        thread_id, f"trivy fs --scanners vuln,misconfig,license --format sarif --output {TRIVY_SARIF_PATH} . 2>&1"
    )
    raw_trivy = await repo_files.read_repo_file(provider, thread_id, TRIVY_SARIF_PATH)
    if raw_trivy is not None:
        findings.extend(parse_sarif(raw_trivy, TRIVY_SEVERITY_MAP))

    sbom_result = await provider.exec_in_sandbox(thread_id, f"trivy fs --format cyclonedx -o {TRIVY_SBOM_PATH} . 2>&1")
    p10["sbom_ok"] = sbom_result.ok

    # --no-git: working-tree-only, fast per-cycle re-scans -- a one-time full-history secret scan
    # is a separate, out-of-scope item (flagged explicitly by the plan, not silently dropped).
    await provider.exec_in_sandbox(thread_id, f"gitleaks detect --report-format json --report-path {GITLEAKS_REPORT_PATH} --no-git 2>&1")
    raw_gitleaks = await repo_files.read_repo_file(provider, thread_id, GITLEAKS_REPORT_PATH)
    if raw_gitleaks is not None:
        findings.extend(_parse_gitleaks(raw_gitleaks))

    existing_keys = set(p10["decisions"].keys())
    p10["findings"] = [f.to_dict() for f in findings if f.finding_key not in existing_keys] + [
        f for f in p10["findings"] if f["finding_key"] in existing_keys
    ]

    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "p10", "node": "scan", "finding_count": len(p10["findings"]), "sbom_ok": p10["sbom_ok"]}
    )
    if p10["sbom_ok"]:
        await git_ops.commit_paths(provider, thread_id, [TRIVY_SBOM_PATH], "ai-dev-workflow: p10 SBOM")
    return {"p10": p10}


async def p10_triage_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p10 = dict(state.get("p10") or default_p10_state())

    open_findings = [f for f in p10["findings"] if f["finding_key"] not in p10["decisions"]]
    if not open_findings:
        return {"p10": p10}

    model = get_chat_model_for_thread(
        thread_id,
        "p10-security",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("p10-security", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=["builtin:view", "builtin:grep", "builtin:glob", "builtin:task_complete", "builtin:ask_user", "builtin:skill"],
    )
    prompt = (
        "Use the `security-triage` skill and, where relevant, the `security-review` skill's "
        "reasoning. NEVER-SUPPRESS RULE: any finding with tool=gitleaks (a leaked secret) must be "
        "decision=fix (rotate/remove) unless you can prove the value is an already-rotated, "
        "non-functional test fixture -- this is the single highest-risk rubber-stamp target. For "
        "every other finding, decide fix or suppress with specific, rule-aware, exploitability-"
        "based reasoning (never a rubber stamp). Findings:\n\n" + json.dumps(open_findings, indent=2)
    )
    response = await ainvoke_structured(
        model, [SystemMessage(content="You are the Code Security Triage Agent."), HumanMessage(content=prompt)], TriageResponse
    )

    decisions = dict(p10["decisions"])
    findings_by_key = {f["finding_key"]: f for f in p10["findings"]}
    for decision in response.decisions:
        finding = findings_by_key.get(decision.finding_key)
        if finding and finding["tool"] == "gitleaks" and decision.decision == "suppress":
            # Never-suppress rule enforced deterministically, not just by prompt instruction --
            # a triage response can't override this by asserting it anyway.
            continue
        decisions[decision.finding_key] = {
            "decision": decision.decision,
            "justification": decision.justification,
            "suppression_marker": decision.suppression_marker,
        }
    p10["decisions"] = decisions
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "p10", "node": "triage", "decision_count": len(response.decisions), "token_usage": model._last_usage}
    )
    return {"p10": p10}


async def p10_ledger_write_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p10 = dict(state.get("p10") or default_p10_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p10": p10}

    provider = get_sandbox_provider()
    findings_by_key = {f["finding_key"]: f for f in p10["findings"]}
    decisions = dict(p10["decisions"])

    for finding_key, decision in decisions.items():
        if decision["decision"] != "suppress" or "ref" in decision:
            continue
        finding = findings_by_key.get(finding_key)
        if finding is None:
            continue
        finding_obj = Finding(**{k: v for k, v in finding.items() if k != "status"}, status=finding.get("status", "open"))
        ref = await append_suppression(provider, thread_id, "p10", finding_obj, decision["justification"])
        decisions[finding_key] = {**decision, "ref": ref}

    p10["decisions"] = decisions
    await git_ops.commit_paths(provider, thread_id, [".ai-dev-workflow/suppressions.md"], "ai-dev-workflow: p10 suppressions")
    return {"p10": p10}


async def p10_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p10 = dict(state.get("p10") or default_p10_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p10": p10}

    to_fix = [
        {**f, **p10["decisions"].get(f["finding_key"], {})}
        for f in p10["findings"]
        if p10["decisions"].get(f["finding_key"], {}).get("decision") == "fix"
    ]
    to_suppress = [
        {**f, **p10["decisions"].get(f["finding_key"], {})}
        for f in p10["findings"]
        if p10["decisions"].get(f["finding_key"], {}).get("decision") == "suppress"
    ]

    if to_fix or to_suppress:
        model = get_chat_model_for_thread(
            thread_id,
            "plan",
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("plan", "draft"),
            sandbox=sandbox_registry.get(thread_id),
            agent_mode="autopilot",
        )
        suppress_instructions = [
            f"{s['file']}:{s.get('line')} -- insert exactly: {s['suppression_marker']} -- ref:{s.get('ref', 'MISSING')}"
            for s in to_suppress
        ]
        prompt = (
            "Fix these security findings (upgrade-first bias for dependency vulnerabilities; "
            f"rotate/remove for any secret):\n\n{json.dumps(to_fix, indent=2)}\n\n"
            f"For these, insert exactly the given suppression marker verbatim:\n\n{json.dumps(suppress_instructions, indent=2)}"
        )
        await model.ainvoke([SystemMessage(content="You are the Code Security Fix Agent."), HumanMessage(content=prompt)])
        await repo_files.append_ledger_entry(
            get_sandbox_provider(), thread_id, {"stage": "p10", "node": "fix", "token_usage": model._last_usage}
        )

    return {"p10": p10}


def make_p10_route_after_gate():
    def route(state: dict[str, Any]) -> str:
        p10 = state.get("p10") or default_p10_state()
        report = p10.get("last_gate_report") or {}
        if report.get("passed"):
            return "next"
        if p10["cycle_count"] < P10_MAX_CYCLES:
            return "retry"
        return "escalate"

    return route


async def p10_gate_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p10 = dict(state.get("p10") or default_p10_state())

    if sandbox_registry.get(thread_id) is None or p10["baseline_commit"] is None:
        p10["last_gate_report"] = {"passed": True}
        return {"p10": p10}

    provider = get_sandbox_provider()
    no_silent = await check_no_silent_suppression(provider, thread_id, p10["baseline_commit"])

    unsuppressed = [
        f for f in p10["findings"]
        if meets_or_exceeds(f["severity"], P10_SEVERITY_FLOOR)
        and p10["decisions"].get(f["finding_key"], {}).get("decision") != "suppress"
    ]

    # SBOM generation failure is a hard infra assertion, routed to escalation on its own --
    # distinct from a severity-bar failure, per the plan's explicit design.
    passed = p10["sbom_ok"] and not unsuppressed and no_silent.passed
    report = {
        "passed": passed,
        "unsuppressed": [f["finding_key"] for f in unsuppressed],
        "sbom_ok": p10["sbom_ok"],
        "severity_floor": P10_SEVERITY_FLOOR,
        "no_silent_suppression": {"bare_markers": no_silent.bare_markers, "dangling_refs": no_silent.dangling_refs},
    }
    p10["last_gate_report"] = report
    if not passed:
        p10["cycle_count"] = p10["cycle_count"] + 1

    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p10", "node": "gate_check", **report})
    return {"p10": p10}


async def p10_human_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    p10 = state.get("p10") or default_p10_state()
    interrupt({"stage": "p10", "type": "security_cycle_cap_exceeded", "report": p10.get("last_gate_report")})
    reset = dict(p10)
    reset["cycle_count"] = 0
    return {"p10": reset}
