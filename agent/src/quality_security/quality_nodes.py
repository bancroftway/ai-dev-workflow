"""quality-remediation -- code quality: a bespoke node cluster (not a StageSpec), wired directly into
build_graph() because it needs a scan -> triage -> fix -> R -> (loop | human gate) cycle neither
the generic StageSpec template nor RebuildSpec express on their own.

Chain: quality_scan -> quality_triage -> quality_ledger_write -> quality_fix -> R(quality_remediation) -> quality_gate_check ->
(loop to quality_scan | quality_human_gate).

The non-.NET half of the scan is delegated to src/repo_scan.py's `quality` profile (jscpd
duplication + lizard per-function complexity), so there is one implementation of each tool
invocation rather than one per caller. The two `dotnet` commands stay here on purpose: they are
build-coupled, not scan-coupled, and hoisting them into the scanner would drag rebuild.py's
per-stack build resolution along with them for nothing.

Quality findings gate on what *this pipeline introduced*, measured against the baseline scan taken
at the top of the graph -- a brownfield repo's pre-existing complexity debt is reported on the
dashboard and burned down over time, not treated as a reason its first gate can never pass. With
no baseline (a greenfield repo, or a repo predating repo_scan) every finding gates, which is the
same rule. Analyzer errors and the duplication threshold remain absolute, exactly as before.

Verification status, stated plainly: this module has NOT been exercised against a real sandbox.
The exact analyzer invocation (dotnet build's SARIF ErrorLog path, dotnet format's report format)
is written to the best of available documentation, not confirmed live -- unlike brownfield-baseline/P1/P2/P4's node
clusters, all of which were verified against a real running container. The sandbox image does not
ship any SonarAnalyzer package reference (SonarAnalyzer.CSharp is a NuGet package a target .NET
repo would need itself, not something the sandbox image installs).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from ..prompt_loader import load_prompt_pair, render_prompt
from langchain_core.runnables import RunnableConfig

from .. import config as workflow_config
from .. import git_ops, model_config, repo_files, repo_scan
from ..copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from ..sandbox import registry as sandbox_registry
from ..sandbox.factory import get_sandbox_provider
from .sarif import Finding, parse_sarif
from .schemas import TriageResponse
from .suppressions import append_suppression, check_no_silent_suppression

QUALITY_MAX_CYCLES = int(os.environ.get("QUALITY_MAX_CYCLES", "3"))
QUALITY_MAX_DUPLICATION_PERCENT = float(os.environ.get("QUALITY_MAX_DUPLICATION_PERCENT", "3.0"))

_SEVERITY_MAP = {"error": "error", "warning": "warning", "note": "info", "none": "info"}

SARIF_PATH = "agent-work/analyzers.sarif"
FORMAT_REPORT_PATH = "agent-work/format-report.json"

# Categories gated on the baseline delta rather than absolutely. `duplication` is deliberately not
# here: it is already gated absolutely by QUALITY_MAX_DUPLICATION_PERCENT below, and counting it twice
# would just make one threshold breach fail two checks.
QUALITY_GATE_CATEGORIES = frozenset({"maintainability"})


class QualityRemediationState(TypedDict):
    cycle_count: int
    findings: list[dict[str, Any]]
    decisions: dict[str, dict[str, Any]]  # finding_key -> {decision, justification, ref}
    duplication_percent: float | None
    format_clean: bool | None
    baseline_commit: str | None
    # Quality finding ids present in the repo *before* this pipeline touched it. None means no
    # baseline was recorded, in which case every quality finding gates.
    baseline_quality_ids: list[str] | None
    build_ok: bool
    last_gate_report: dict[str, Any] | None


def default_quality_state() -> QualityRemediationState:
    return {
        "cycle_count": 0,
        "findings": [],
        "decisions": {},
        "duplication_percent": None,
        "format_clean": None,
        "baseline_commit": None,
        "baseline_quality_ids": None,
        "build_ok": True,
        "last_gate_report": None,
    }


async def _baseline_quality_ids(provider: Any, thread_id: str) -> list[str] | None:
    raw = await repo_files.read_repo_file(provider, thread_id, repo_scan.BASELINE_PATH)
    if raw is None:
        return None
    try:
        baseline = json.loads(raw)
    except json.JSONDecodeError:
        return None
    findings = baseline.get("findings")
    if findings is None:
        return None
    return sorted(f["id"] for f in findings if f.get("category") in QUALITY_GATE_CATEGORIES)


async def quality_scan_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = dict(state.get("quality_remediation") or default_quality_state())

    if sandbox_registry.get(thread_id) is None:
        quality_remediation["build_ok"] = True
        return {"quality_remediation": quality_remediation}

    provider = get_sandbox_provider()
    if quality_remediation["baseline_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        quality_remediation["baseline_commit"] = head.stdout.strip() if head.ok else None

    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}

    findings: list[Finding] = []
    if tech_stack.get("dotnet_detected"):
        build_result = await provider.exec_in_sandbox(
            thread_id,
            "mkdir -p agent-work && dotnet build --no-incremental "
            "\"/p:ErrorLog=agent-work/analyzers.sarif%2Cversion=2\" 2>&1",
        )
        quality_remediation["build_ok"] = build_result.ok
        if not build_result.ok:
            # A non-compiling tree can't be trusted for analyzer coverage -- short-circuits to
            # human escalation via quality_gate_check's own routing (never loops scan->triage->fix on
            # a tree that doesn't even build).
            await repo_files.append_ledger_entry(
                provider, thread_id, {"stage": "quality_remediation", "node": "scan", "build_ok": False}
            )
            return {"quality_remediation": quality_remediation}

        raw_sarif = await repo_files.read_repo_file(provider, thread_id, SARIF_PATH)
        if raw_sarif is not None:
            findings.extend(parse_sarif(raw_sarif, _SEVERITY_MAP))

        format_result = await provider.exec_in_sandbox(
            thread_id, "dotnet format --verify-no-changes --report agent-work/format-report.json 2>&1"
        )
        quality_remediation["format_clean"] = format_result.ok
    else:
        quality_remediation["build_ok"] = True
        quality_remediation["format_clean"] = True

    # jscpd (duplication) + lizard (per-function complexity), through the shared scanner so these
    # invocations live in exactly one place. include_metrics=True because the duplication *number*
    # is what the gate compares, not just the finding. report_path keeps repo-scan-latest.json
    # fresh so the frontend metrics bar has stage-end numbers before metrics-report runs.
    scan = await repo_scan.run_repo_scan(
        provider, thread_id, profile="quality", include_metrics=True, report_path=repo_scan.LATEST_PATH
    )
    await git_ops.commit_paths(provider, thread_id, [repo_scan.LATEST_PATH], "ai-dev-workflow: quality scan snapshot")
    findings.extend(scan.findings)
    quality_remediation["duplication_percent"] = (scan.metrics.get("duplication") or {}).get("percent")

    if quality_remediation["baseline_quality_ids"] is None:
        quality_remediation["baseline_quality_ids"] = await _baseline_quality_ids(provider, thread_id)

    # Only findings not already decided this run carry forward for triage -- bounds token cost
    # across loop iterations, per the plan's own design intent.
    # Current scan is the only truth -- see security_nodes.py's twin comment: keeping decided
    # findings forever meant a fixed finding never left the list and the gate never converged.
    quality_remediation["findings"] = [f.to_dict() for f in findings]

    await repo_files.append_ledger_entry(
        provider,
        thread_id,
        {"stage": "quality_remediation", "node": "scan", "build_ok": quality_remediation["build_ok"], "finding_count": len(quality_remediation["findings"]), "duplication_percent": quality_remediation["duplication_percent"]},
    )
    # repo_scan is a LastValue channel -- spread prior state (see repo_scan_baseline_node).
    prior_repo_scan = dict(state.get("repo_scan") or {})
    prior_repo_scan["latest_summary"] = scan.to_dashboard_dict()["summary"]
    prior_repo_scan["latest_duplication_percent"] = quality_remediation["duplication_percent"]
    return {"quality_remediation": quality_remediation, "repo_scan": prior_repo_scan}


async def quality_triage_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = dict(state.get("quality_remediation") or default_quality_state())

    open_findings = [f for f in quality_remediation["findings"] if f["finding_key"] not in quality_remediation["decisions"]]
    if not open_findings:
        return {"quality_remediation": quality_remediation}

    model = get_chat_model_for_thread(
        thread_id,
        "quality-remediation",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("quality-remediation", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    system, template = load_prompt_pair("quality_remediation_triage")
    prompt = render_prompt(template, findings_json=json.dumps(open_findings, indent=2))
    response = await ainvoke_structured(
        model, [SystemMessage(content=system), HumanMessage(content=prompt)], TriageResponse
    )

    decisions = dict(quality_remediation["decisions"])
    for decision in response.decisions:
        decisions[decision.finding_key] = {
            "decision": decision.decision,
            "justification": decision.justification,
            "suppression_marker": decision.suppression_marker,
        }
    quality_remediation["decisions"] = decisions
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "quality_remediation", "node": "triage", "decision_count": len(response.decisions), "token_usage": model._last_usage}
    )
    return {"quality_remediation": quality_remediation}


async def quality_ledger_write_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Deterministic: for every `suppress` decision without a `ref` yet, appends a suppressions.md
    row built from the Finding's own deterministic data + the LLM's justification string, and
    stores the returned ref back onto the decision -- runs before quality_fix so the fix node only ever
    inserts already-ledger-backed suppression markers."""
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = dict(state.get("quality_remediation") or default_quality_state())
    if sandbox_registry.get(thread_id) is None:
        return {"quality_remediation": quality_remediation}

    provider = get_sandbox_provider()
    findings_by_key = {f["finding_key"]: f for f in quality_remediation["findings"]}
    decisions = dict(quality_remediation["decisions"])

    wrote_any = False
    for finding_key, decision in decisions.items():
        if decision["decision"] != "suppress" or "ref" in decision:
            continue
        finding = findings_by_key.get(finding_key)
        if finding is None:
            continue
        finding_obj = Finding(**{k: v for k, v in finding.items() if k != "status"}, status=finding.get("status", "open"))
        ref = await append_suppression(provider, thread_id, "quality_remediation", finding_obj, decision["justification"])
        decisions[finding_key] = {**decision, "ref": ref}
        wrote_any = True

    quality_remediation["decisions"] = decisions
    # Commit only when something was written: with zero suppressions the file never exists and
    # `git add` on the pathspec is a hard error (observed live -- crashed the run).
    if wrote_any:
        await git_ops.commit_paths(provider, thread_id, [".ai-dev-workflow/suppressions.md"], "ai-dev-workflow: quality_remediation suppressions")
    return {"quality_remediation": quality_remediation}


async def quality_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = dict(state.get("quality_remediation") or default_quality_state())
    if sandbox_registry.get(thread_id) is None:
        return {"quality_remediation": quality_remediation}

    provider = get_sandbox_provider()
    # Mechanical auto-fixes first, as plain shell -- no LLM budget spent on formatting.
    if quality_remediation["format_clean"] is False:
        await provider.exec_in_sandbox(thread_id, "dotnet format 2>&1")

    to_fix = [
        {**f, **quality_remediation["decisions"].get(f["finding_key"], {})}
        for f in quality_remediation["findings"]
        if quality_remediation["decisions"].get(f["finding_key"], {}).get("decision") == "fix"
    ]
    to_suppress = [
        {**f, **quality_remediation["decisions"].get(f["finding_key"], {})}
        for f in quality_remediation["findings"]
        if quality_remediation["decisions"].get(f["finding_key"], {}).get("decision") == "suppress"
    ]

    if to_fix or to_suppress:
        # Own session key (quality_remediation-fix:draft), not plan:draft -- sharing it returned plan's cached
        # read-only session so this autopilot fixer silently couldn't write. The fixer uses this
        # STAGE's model (falling back to plan's): fixing is codegen-tier work, and hardcoding
        # plan's model silently downgraded it when plan moved to a mini roster (observed live:
        # 37-token fix replies that fixed nothing).
        model = get_chat_model_for_thread(
            thread_id,
            "quality_remediation-fix",
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("quality-remediation", "draft") or model_config.get_model_name("plan", "draft"),
            sandbox=sandbox_registry.get(thread_id),
            agent_mode="autopilot",
        )
        suppress_instructions = [
            f"{s['file']}:{s.get('line')} -- insert exactly: {s['suppression_marker']} -- ref:{s.get('ref', 'MISSING')}"
            for s in to_suppress
        ]
        system, template = load_prompt_pair("quality_remediation_fix")
        prompt = render_prompt(
            template,
            to_fix_json=json.dumps(to_fix, indent=2),
            suppress_instructions_json=json.dumps(suppress_instructions, indent=2),
        )
        await model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        await repo_files.append_ledger_entry(provider, thread_id, {"stage": "quality_remediation", "node": "fix", "token_usage": model._last_usage})
        await git_ops.commit_all(provider, thread_id, "ai-dev-workflow: quality-remediation code fixes")

    return {"quality_remediation": quality_remediation}


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    escalate: bool
    report: dict[str, Any]


def make_quality_route_after_gate():
    def route(state: dict[str, Any]) -> str:
        quality_remediation = state.get("quality_remediation") or default_quality_state()
        report = quality_remediation.get("last_gate_report") or {}
        if report.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if report.get("passed"):
            return "next"
        if not quality_remediation.get("build_ok", True):
            return "escalate"  # non-compiling tree -- never loop, always a human decision
        if quality_remediation["cycle_count"] < QUALITY_MAX_CYCLES:
            return "retry"
        return "escalate"

    return route


async def quality_gate_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = dict(state.get("quality_remediation") or default_quality_state())

    if sandbox_registry.get(thread_id) is None:
        # No sandbox means the quality gate could not actually run. Failing OPEN here would let a
        # run report green having checked nothing -- escalate to a human instead (route reads
        # cannot_verify).
        quality_remediation["last_gate_report"] = {"passed": False, "cannot_verify": True, "reason": "no sandbox -- quality gate did not run"}
        return {"quality_remediation": quality_remediation}
    if quality_remediation["baseline_commit"] is None:
        quality_remediation["last_gate_report"] = {"passed": True}
        return {"quality_remediation": quality_remediation}

    provider = get_sandbox_provider()
    no_silent = await check_no_silent_suppression(provider, thread_id, quality_remediation["baseline_commit"])

    def _unsuppressed(finding: dict[str, Any]) -> bool:
        return quality_remediation["decisions"].get(finding["finding_key"], {}).get("decision") != "suppress"

    unsuppressed_errors = [f for f in quality_remediation["findings"] if f["severity"] == "error" and _unsuppressed(f)]

    # Introduced-only, per the module docstring. A corroborated finding is still one real finding
    # here -- dedup collapses duplicates, never the issue itself, so this can't drop a count below
    # the bar and pass a tree that should have failed.
    baseline_ids = quality_remediation.get("baseline_quality_ids")
    quality_findings = [f for f in quality_remediation["findings"] if f.get("category", "sast") in QUALITY_GATE_CATEGORIES]
    introduced_quality = [
        f for f in quality_findings
        if (baseline_ids is None or f["finding_key"] not in baseline_ids) and _unsuppressed(f)
    ]

    duplication_ok = quality_remediation["duplication_percent"] is None or quality_remediation["duplication_percent"] <= QUALITY_MAX_DUPLICATION_PERCENT

    passed = (
        quality_remediation["build_ok"]
        and not unsuppressed_errors
        and not introduced_quality
        and (quality_remediation["format_clean"] is not False)
        and duplication_ok
        and no_silent.passed
    )
    report = {
        "passed": passed,
        "unsuppressed_errors": [f["finding_key"] for f in unsuppressed_errors],
        "introduced_quality_findings": [f["finding_key"] for f in introduced_quality],
        "pre_existing_quality_findings": len(quality_findings) - len(introduced_quality),
        "quality_gate_scope": "introduced_only" if baseline_ids is not None else "absolute_no_baseline",
        "format_clean": quality_remediation["format_clean"],
        "duplication_percent": quality_remediation["duplication_percent"],
        "no_silent_suppression": {"bare_markers": no_silent.bare_markers, "dangling_refs": no_silent.dangling_refs},
    }
    quality_remediation["last_gate_report"] = report
    if not passed:
        quality_remediation["cycle_count"] = quality_remediation["cycle_count"] + 1

    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "quality_remediation", "node": "gate_check", **report})
    return {"quality_remediation": quality_remediation}


async def quality_human_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Cap-hit terminal failure -- quality-remediation never auto-approves past unresolved quality
    findings, and never pauses for a human either: the run ENDs with run_failure set. The counter
    reset rides in the same return so the next resubmission starts fresh."""
    thread_id = config["configurable"]["thread_id"]
    quality_remediation = state.get("quality_remediation") or default_quality_state()
    payload = {"stage": "quality_remediation", "type": "quality_cycle_cap_exceeded", "report": quality_remediation.get("last_gate_report")}
    await git_ops.record_run_failure(thread_id, payload, state.get("run_id"))
    reset = dict(quality_remediation)
    reset["cycle_count"] = 0
    return {"quality_remediation": reset, "run_failure": payload}
