"""audit-cluster's deterministic hooks/gates: dedup-simplify's post-dedup jscpd re-check, license-audit's confidence-gated
license routing, and audit-cluster's own exit gate (re-verifies coverage/duplication/license policy, never
assumes any of the three still hold from an earlier stage's own check).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig

from .. import git_ops, repo_files, repo_scan
from ..sandbox import registry as sandbox_registry
from ..sandbox.factory import get_sandbox_provider
from ..sandbox.provider import SandboxProvider
from .test_coverage_gate import MIN_COVERAGE_PERCENT_DEFAULT, verify_coverage

if TYPE_CHECKING:
    from ..graph import GraphState, VerificationResult

AUDIT_MAX_DUPLICATION_PERCENT = float(os.environ.get("AUDIT_MAX_DUPLICATION_PERCENT", "3.0"))
LICENSE_POLICY_PATH = "license-policy.json"
THIRD_PARTY_NOTICES_PATH = "THIRD-PARTY-NOTICES.md"
LICENSE_APPROVALS_PATH = "audit/license-approvals.json"


async def _write_license_approvals(
    provider: SandboxProvider, thread_id: str, run_id: str, classifications: list[dict[str, Any]], flagged: list[dict[str, Any]]
) -> None:
    """Deterministic record of every package this stage has ever flagged for human review, keyed
    by package name so re-drafts (license-audit has max_verify_cycles=0, so any flagged package escalates
    immediately) don't lose earlier history. A package's decision starts "pending_human_review" and
    is never flipped to "auto_approved" here on the strength of a later pass simply not flagging it
    again -- the interrupt/resume mechanism this graph uses doesn't carry a structured decision
    payload back from the human (see make_escalate_node), so this file records what was surfaced
    for review, not a fabricated approve/deny this code never actually observed."""
    raw = await repo_files.read_repo_file(provider, thread_id, LICENSE_APPROVALS_PATH)
    try:
        approvals = json.loads(raw) if raw else {"packages": {}}
    except json.JSONDecodeError:
        approvals = {"packages": {}}
    flagged_names = {f.get("package_name") for f in flagged}
    for c in classifications:
        name = c.get("package_name")
        if not name:
            continue
        entry = approvals["packages"].get(name, {})
        entry.update(
            {
                "ecosystem": c.get("ecosystem"),
                "detected_license": c.get("detected_license"),
                "bucket": c.get("bucket"),
                "confidence": c.get("confidence"),
                "dual_or_exception_flag": c.get("dual_or_exception_flag", False),
                "rationale": c.get("rationale"),
                "last_seen_run_id": run_id,
            }
        )
        if name in flagged_names:
            entry["decision"] = "pending_human_review"
        else:
            entry.setdefault("decision", "auto_approved")
        approvals["packages"][name] = entry
    await repo_files.write_repo_file(provider, thread_id, LICENSE_APPROVALS_PATH, json.dumps(approvals, indent=2) + "\n")


async def _run_jscpd(provider: SandboxProvider, thread_id: str) -> float | None:
    """Through the shared scanner (src/repo_scan.py), so jscpd's invocation and report parsing
    exist in one place rather than here *and* in quality-remediation's scan node."""
    scan = await repo_scan.run_repo_scan(provider, thread_id, tools=["jscpd"], include_metrics=True)
    return (scan.metrics.get("duplication") or {}).get("percent")


async def rerun_jscpd_after_dedup(thread_id: str, content_dict: dict[str, Any], _state: "GraphState", provider: SandboxProvider) -> None:
    """StageSpec.post_approve_hook for dedup-simplify: re-runs jscpd deterministically once the
    refactor is approved, and writes the result directly into content_dict['duplication_percent_after']
    -- never asks the model to self-report this number."""
    content_dict["duplication_percent_after"] = await _run_jscpd(provider, thread_id)


async def verify_license_audit(
    thread_id: str, content_dict: dict[str, Any], run_id: str, _baseline_commit: str | None, provider: SandboxProvider
) -> "VerificationResult":
    """license-audit's deterministic_verify. Combined with max_verify_cycles=0 on the StageSpec (see
    graph.py's wiring), this makes ANY flagged classification escalate straight to a human gate on
    the first check -- re-drafting won't change reality about a package's actual license, so
    there's no point burning a retry cycle before asking a human. Also renders
    THIRD-PARTY-NOTICES.md deterministically -- never the model's job."""
    from ..graph import VerificationResult

    classifications = content_dict.get("classifications") or []
    flagged = [
        c for c in classifications
        if c.get("confidence") == "low" or c.get("bucket") in ("deny", "review_required", "unknown") or c.get("dual_or_exception_flag")
    ]

    lines = ["# Third-Party Notices", "", "Auto-generated by ai-dev-workflow's audit-cluster license audit -- do not hand-edit.", ""]
    for c in classifications:
        lines.append(f"- **{c.get('package_name', '')}** ({c.get('ecosystem', '')}): {c.get('detected_license', '')} -- {c.get('bucket', '')}")
    await repo_files.write_repo_file(provider, thread_id, THIRD_PARTY_NOTICES_PATH, "\n".join(lines).strip() + "\n")
    await _write_license_approvals(provider, thread_id, run_id, classifications, flagged)

    if flagged:
        return VerificationResult(
            passed=False,
            feedback=f"{len(flagged)} package(s) require human review (low confidence, deny/review-required/unknown bucket, or dual/exception license): {[f['package_name'] for f in flagged]}",
            report={"flagged": flagged, "all_classifications": classifications},
        )
    return VerificationResult(passed=True, feedback="All packages high-confidence and allowed.", report={"all_classifications": classifications})


@dataclass(frozen=True)
class AuditExitOutcome:
    passed: bool
    reasons: list[str]
    report: dict[str, Any]


async def verify_audit_exit(thread_id: str, state: dict[str, Any], provider: SandboxProvider) -> AuditExitOutcome:
    """audit-cluster's own exit gate: re-verifies (never assumes) coverage still >=95%, duplication under
    threshold, and license policy passes. On failure, the caller does one automatic re-run of
    just the failing check (covers tool flake) before a hard human-gate escalation -- see
    make_audit_route_after_exit_gate in audit_nodes.py for that retry-once semantics.
    """
    reasons: list[str] = []
    report: dict[str, Any] = {}

    coverage_result = await verify_coverage(thread_id, {}, "audit_cluster-exit", None, provider)
    report["coverage"] = coverage_result.report
    if not coverage_result.passed:
        reasons.append(f"coverage regressed: {coverage_result.feedback}")

    duplication_percent = await _run_jscpd(provider, thread_id)
    report["duplication_percent"] = duplication_percent
    if duplication_percent is not None and duplication_percent > AUDIT_MAX_DUPLICATION_PERCENT:
        reasons.append(f"duplication {duplication_percent}% exceeds {AUDIT_MAX_DUPLICATION_PERCENT}% threshold")

    raw_policy = await repo_files.read_repo_file(provider, thread_id, LICENSE_POLICY_PATH)
    if raw_policy is not None:
        try:
            policy = json.loads(raw_policy)
            deny_list = set(policy.get("deny", []))
            raw_license_state = (state.get("stages") or {}).get("license-audit", {})
            classifications = ((raw_license_state.get("approved_content") or {}).get("classifications")) or []
            denied_hits = [c["package_name"] for c in classifications if c.get("package_name") in deny_list]
            if denied_hits:
                reasons.append(f"denylisted packages present with no recorded override: {denied_hits}")
        except json.JSONDecodeError:
            pass

    return AuditExitOutcome(passed=not reasons, reasons=reasons, report=report)


async def dedup_simplify_pre_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Deterministic pre-node run before dedup-simplify's own draft: gives the drafting LLM jscpd's actual
    duplication-cluster report as grounding context, rather than trusting it to explore and find
    duplication unaided."""
    thread_id = config["configurable"]["thread_id"]
    audit_cluster = dict(state.get("audit_cluster") or {})
    if sandbox_registry.get(thread_id) is None:
        audit_cluster["jscpd_report_for_dedup"] = "(no sandbox -- nothing to scan)"
        return {"audit_cluster": audit_cluster}
    provider = get_sandbox_provider()
    scan = await repo_scan.run_repo_scan(provider, thread_id, tools=["jscpd"], include_metrics=True)
    duplication = scan.metrics.get("duplication") or {}
    # The parsed clone list rather than a truncated console tail: same underlying report, but the
    # model gets file/line pairs it can act on instead of 6000 characters cut mid-table.
    audit_cluster["jscpd_report_for_dedup"] = json.dumps(duplication, indent=2) if duplication else "(jscpd produced no report)"
    return {"audit_cluster": audit_cluster}


def _resolve_license_scan_command(tech_stack: dict[str, Any]) -> str | None:
    if tech_stack.get("dotnet_detected"):
        # Best-effort -- requires the nuget-license dotnet tool to already be available; it is
        # pulled as a `dotnet tool` in the target repo at scan time, not baked into the image
        # (no target repo exists at image-build time), so absence here is tolerated, not fatal.
        return "dotnet tool run nuget-license --output json 2>&1"
    languages = [str(l).lower() for l in (tech_stack.get("languages") or [])]
    if "typescript" in languages or "javascript" in languages:
        # --excludePrivatePackages: license policy governs THIRD-PARTY dependencies. Without it
        # the repo's own private/UNLICENSED root package appears in the scan and the license
        # verify flags the repo for containing itself (observed live, run 20 -- an unresolvable
        # END since redrafting cannot relicense the repo under audit).
        return "npx --yes license-checker --json --excludePrivatePackages 2>&1"
    return None


async def license_audit_pre_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Deterministic pre-node run before license-audit's own draft: the LLM classifies confidence/bucket
    from a real scanner's declared/detected license data, it never runs the scanner itself."""
    thread_id = config["configurable"]["thread_id"]
    audit_cluster = dict(state.get("audit_cluster") or {})
    if sandbox_registry.get(thread_id) is None:
        audit_cluster["license_scan_report"] = "(no sandbox -- nothing to scan)"
        return {"audit_cluster": audit_cluster}
    provider = get_sandbox_provider()
    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}
    command = _resolve_license_scan_command(tech_stack)
    if command is None:
        audit_cluster["license_scan_report"] = "(no license-scan command mapping for this stack)"
        return {"audit_cluster": audit_cluster}
    result = await provider.exec_in_sandbox(thread_id, command)
    audit_cluster["license_scan_report"] = (result.stdout or result.stderr or "(scan produced no output)")[-8000:]
    return {"audit_cluster": audit_cluster}


async def audit_exit_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """audit-cluster's own exit gate node. On failure: one automatic re-run of just the failing check
    (covers tool flake) before a hard human-gate escalation -- never an automatic loop back into
    the audit-cluster sub-stages themselves (bounds worst-case runtime), per the plan's explicit design.
    """
    thread_id = config["configurable"]["thread_id"]
    audit_cluster = dict(state.get("audit_cluster") or {"attempt_count": 0, "last_outcome": None})
    if sandbox_registry.get(thread_id) is None:
        # No sandbox means the audit-cluster exit checks (coverage, dedup, license) could not run. Escalate
        # rather than pass green (route reads cannot_verify).
        audit_cluster["last_outcome"] = {"passed": False, "cannot_verify": True, "reasons": ["no sandbox -- audit-cluster exit gate did not run"], "report": {}}
        return {"audit_cluster": audit_cluster}

    provider = get_sandbox_provider()
    outcome = await verify_audit_exit(thread_id, state, provider)
    audit_cluster["last_outcome"] = {"passed": outcome.passed, "reasons": outcome.reasons, "report": outcome.report}
    if not outcome.passed:
        audit_cluster["attempt_count"] = audit_cluster.get("attempt_count", 0) + 1
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "audit_cluster", "node": "exit_gate", **audit_cluster["last_outcome"]})
    return {"audit_cluster": audit_cluster}


def make_audit_exit_route():
    def route(state: dict[str, Any]) -> str:
        audit_cluster = state.get("audit_cluster") or {"attempt_count": 0, "last_outcome": None}
        last = audit_cluster.get("last_outcome") or {}
        if last.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if last.get("passed"):
            return "next"
        # Exactly one automatic re-check before escalating -- attempt_count reaches 2 on the
        # second consecutive failure.
        if audit_cluster.get("attempt_count", 0) < 2:
            return "retry"
        return "escalate"

    return route


async def audit_exit_human_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Two consecutive exit-gate failures END the run with run_failure set (no human pause --
    spec/plan approval are the only human touchpoints). Counter reset rides in the same return."""
    thread_id = config["configurable"]["thread_id"]
    audit_cluster = state.get("audit_cluster") or {"attempt_count": 0, "last_outcome": None}
    payload = {"stage": "audit_cluster", "type": "exit_gate_failed_twice", "outcome": audit_cluster.get("last_outcome")}
    await git_ops.record_run_failure(thread_id, payload, state.get("run_id"))
    reset = dict(audit_cluster)
    reset["attempt_count"] = 0
    return {"audit_cluster": reset, "run_failure": payload}
