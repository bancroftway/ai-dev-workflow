"""P8 -- code quality: a bespoke node cluster (not a StageSpec), wired directly into
build_graph() because it needs a scan -> triage -> fix -> R -> (loop | human gate) cycle neither
the generic StageSpec template nor RebuildSpec express on their own.

Chain: p8_scan -> p8_triage -> p8_ledger_write -> p8_fix -> R(p8) -> p8_gate_check ->
(loop to p8_scan | p8_human_gate).

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
is written to the best of available documentation, not confirmed live -- unlike P0/P1/P2/P4's node
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
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from .. import git_ops, model_config, repo_files, repo_scan
from ..copilot_chat_model import ainvoke_structured, get_chat_model_for_thread

# SonarQube MCP -- code-complete, UNVERIFIED (no live SonarQube server in this environment to spike
# against, unlike Playwright MCP which was verified live; same config shape confirmed real via that
# spike). SONARQUBE_URL/SONARQUBE_TOKEN must be set in the sandbox's env for this to actually connect.
SONARQUBE_MCP_CONFIG: dict[str, Any] = {
    "sonarqube": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "sonarqube-mcp-server@latest"],
        "env": {"SONARQUBE_URL": os.environ.get("SONARQUBE_URL", ""), "SONARQUBE_TOKEN": os.environ.get("SONARQUBE_TOKEN", "")},
        "tools": ["*"],
    }
}
from ..sandbox import registry as sandbox_registry
from ..sandbox.factory import get_sandbox_provider
from .sarif import Finding, parse_sarif
from .schemas import TriageResponse
from .suppressions import append_suppression, check_no_silent_suppression

P8_MAX_CYCLES = int(os.environ.get("P8_MAX_CYCLES", "3"))
P8_MAX_DUPLICATION_PERCENT = float(os.environ.get("P8_MAX_DUPLICATION_PERCENT", "3.0"))

_SEVERITY_MAP = {"error": "error", "warning": "warning", "note": "info", "none": "info"}

SARIF_PATH = "agent-work/analyzers.sarif"
FORMAT_REPORT_PATH = "agent-work/format-report.json"

# Categories gated on the baseline delta rather than absolutely. `duplication` is deliberately not
# here: it is already gated absolutely by P8_MAX_DUPLICATION_PERCENT below, and counting it twice
# would just make one threshold breach fail two checks.
QUALITY_GATE_CATEGORIES = frozenset({"maintainability"})


class P8State(TypedDict):
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


def default_p8_state() -> P8State:
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


async def p8_scan_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p8 = dict(state.get("p8") or default_p8_state())

    if sandbox_registry.get(thread_id) is None:
        p8["build_ok"] = True
        return {"p8": p8}

    provider = get_sandbox_provider()
    if p8["baseline_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        p8["baseline_commit"] = head.stdout.strip() if head.ok else None

    raw_tech_stack = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json")
    tech_stack = json.loads(raw_tech_stack) if raw_tech_stack else {}

    findings: list[Finding] = []
    if tech_stack.get("dotnet_detected"):
        build_result = await provider.exec_in_sandbox(
            thread_id,
            "mkdir -p agent-work && dotnet build --no-incremental "
            "\"/p:ErrorLog=agent-work/analyzers.sarif%2Cversion=2\" 2>&1",
        )
        p8["build_ok"] = build_result.ok
        if not build_result.ok:
            # A non-compiling tree can't be trusted for analyzer coverage -- short-circuits to
            # human escalation via p8_gate_check's own routing (never loops scan->triage->fix on
            # a tree that doesn't even build).
            await repo_files.append_ledger_entry(
                provider, thread_id, {"stage": "p8", "node": "scan", "build_ok": False}
            )
            return {"p8": p8}

        raw_sarif = await repo_files.read_repo_file(provider, thread_id, SARIF_PATH)
        if raw_sarif is not None:
            findings.extend(parse_sarif(raw_sarif, _SEVERITY_MAP))

        format_result = await provider.exec_in_sandbox(
            thread_id, "dotnet format --verify-no-changes --report agent-work/format-report.json 2>&1"
        )
        p8["format_clean"] = format_result.ok
    else:
        p8["build_ok"] = True
        p8["format_clean"] = True

    # jscpd (duplication) + lizard (per-function complexity), through the shared scanner so these
    # invocations live in exactly one place. include_metrics=True because the duplication *number*
    # is what the gate compares, not just the finding.
    scan = await repo_scan.run_repo_scan(provider, thread_id, profile="quality", include_metrics=True)
    findings.extend(scan.findings)
    p8["duplication_percent"] = (scan.metrics.get("duplication") or {}).get("percent")

    if p8["baseline_quality_ids"] is None:
        p8["baseline_quality_ids"] = await _baseline_quality_ids(provider, thread_id)

    # Only findings not already decided this run carry forward for triage -- bounds token cost
    # across loop iterations, per the plan's own design intent.
    existing_keys = set(p8["decisions"].keys())
    p8["findings"] = [f.to_dict() for f in findings if f.finding_key not in existing_keys] + [
        f for f in p8["findings"] if f["finding_key"] in existing_keys
    ]

    await repo_files.append_ledger_entry(
        provider,
        thread_id,
        {"stage": "p8", "node": "scan", "build_ok": p8["build_ok"], "finding_count": len(p8["findings"]), "duplication_percent": p8["duplication_percent"]},
    )
    return {"p8": p8}


async def p8_triage_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p8 = dict(state.get("p8") or default_p8_state())

    open_findings = [f for f in p8["findings"] if f["finding_key"] not in p8["decisions"]]
    if not open_findings:
        return {"p8": p8}

    model = get_chat_model_for_thread(
        thread_id,
        "p8-quality",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("p8-quality", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=["builtin:view", "builtin:grep", "builtin:glob", "builtin:task_complete", "builtin:ask_user", "builtin:skill", "mcp:*"],
        mcp_servers=SONARQUBE_MCP_CONFIG,
    )
    prompt = (
        "Use the `quality-triage` skill. For every finding below, decide fix or suppress with a "
        "specific, rule-aware justification (never a rubber stamp under ~15 words). You may query "
        "the SonarQube MCP server for deeper smell/complexity/duplication reasoning on any finding "
        "you're uncertain about. Findings:\n\n"
        + json.dumps(open_findings, indent=2)
    )
    response = await ainvoke_structured(
        model, [SystemMessage(content="You are the Code Quality Triage Agent."), HumanMessage(content=prompt)], TriageResponse
    )

    decisions = dict(p8["decisions"])
    for decision in response.decisions:
        decisions[decision.finding_key] = {
            "decision": decision.decision,
            "justification": decision.justification,
            "suppression_marker": decision.suppression_marker,
        }
    p8["decisions"] = decisions
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "p8", "node": "triage", "decision_count": len(response.decisions), "token_usage": model._last_usage}
    )
    return {"p8": p8}


async def p8_ledger_write_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Deterministic: for every `suppress` decision without a `ref` yet, appends a suppressions.md
    row built from the Finding's own deterministic data + the LLM's justification string, and
    stores the returned ref back onto the decision -- runs before p8_fix so the fix node only ever
    inserts already-ledger-backed suppression markers."""
    thread_id = config["configurable"]["thread_id"]
    p8 = dict(state.get("p8") or default_p8_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p8": p8}

    provider = get_sandbox_provider()
    findings_by_key = {f["finding_key"]: f for f in p8["findings"]}
    decisions = dict(p8["decisions"])

    for finding_key, decision in decisions.items():
        if decision["decision"] != "suppress" or "ref" in decision:
            continue
        finding = findings_by_key.get(finding_key)
        if finding is None:
            continue
        finding_obj = Finding(**{k: v for k, v in finding.items() if k != "status"}, status=finding.get("status", "open"))
        ref = await append_suppression(provider, thread_id, "p8", finding_obj, decision["justification"])
        decisions[finding_key] = {**decision, "ref": ref}

    p8["decisions"] = decisions
    await git_ops.commit_paths(provider, thread_id, [".ai-dev-workflow/suppressions.md"], "ai-dev-workflow: p8 suppressions")
    return {"p8": p8}


async def p8_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p8 = dict(state.get("p8") or default_p8_state())
    if sandbox_registry.get(thread_id) is None:
        return {"p8": p8}

    provider = get_sandbox_provider()
    # Mechanical auto-fixes first, as plain shell -- no LLM budget spent on formatting.
    if p8["format_clean"] is False:
        await provider.exec_in_sandbox(thread_id, "dotnet format 2>&1")

    to_fix = [
        {**f, **p8["decisions"].get(f["finding_key"], {})}
        for f in p8["findings"]
        if p8["decisions"].get(f["finding_key"], {}).get("decision") == "fix"
    ]
    to_suppress = [
        {**f, **p8["decisions"].get(f["finding_key"], {})}
        for f in p8["findings"]
        if p8["decisions"].get(f["finding_key"], {}).get("decision") == "suppress"
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
            "Fix these findings (file/line/rule/message given) -- each fix must genuinely address the "
            f"rule, not just silence it:\n\n{json.dumps(to_fix, indent=2)}\n\n"
            "For these, insert exactly the given suppression marker text at the given location, "
            f"verbatim, nothing else:\n\n{json.dumps(suppress_instructions, indent=2)}"
        )
        await model.ainvoke([SystemMessage(content="You are the Code Quality Fix Agent."), HumanMessage(content=prompt)])
        await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p8", "node": "fix", "token_usage": model._last_usage})

    return {"p8": p8}


@dataclass(frozen=True)
class P8GateResult:
    passed: bool
    escalate: bool
    report: dict[str, Any]


def make_p8_route_after_gate():
    def route(state: dict[str, Any]) -> str:
        p8 = state.get("p8") or default_p8_state()
        report = p8.get("last_gate_report") or {}
        if report.get("passed"):
            return "next"
        if not p8.get("build_ok", True):
            return "escalate"  # non-compiling tree -- never loop, always a human decision
        if p8["cycle_count"] < P8_MAX_CYCLES:
            return "retry"
        return "escalate"

    return route


async def p8_gate_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    p8 = dict(state.get("p8") or default_p8_state())

    if sandbox_registry.get(thread_id) is None or p8["baseline_commit"] is None:
        p8["last_gate_report"] = {"passed": True}
        return {"p8": p8}

    provider = get_sandbox_provider()
    no_silent = await check_no_silent_suppression(provider, thread_id, p8["baseline_commit"])

    def _unsuppressed(finding: dict[str, Any]) -> bool:
        return p8["decisions"].get(finding["finding_key"], {}).get("decision") != "suppress"

    unsuppressed_errors = [f for f in p8["findings"] if f["severity"] == "error" and _unsuppressed(f)]

    # Introduced-only, per the module docstring. A corroborated finding is still one real finding
    # here -- dedup collapses duplicates, never the issue itself, so this can't drop a count below
    # the bar and pass a tree that should have failed.
    baseline_ids = p8.get("baseline_quality_ids")
    quality_findings = [f for f in p8["findings"] if f.get("category", "sast") in QUALITY_GATE_CATEGORIES]
    introduced_quality = [
        f for f in quality_findings
        if (baseline_ids is None or f["finding_key"] not in baseline_ids) and _unsuppressed(f)
    ]

    duplication_ok = p8["duplication_percent"] is None or p8["duplication_percent"] <= P8_MAX_DUPLICATION_PERCENT

    passed = (
        p8["build_ok"]
        and not unsuppressed_errors
        and not introduced_quality
        and (p8["format_clean"] is not False)
        and duplication_ok
        and no_silent.passed
    )
    report = {
        "passed": passed,
        "unsuppressed_errors": [f["finding_key"] for f in unsuppressed_errors],
        "introduced_quality_findings": [f["finding_key"] for f in introduced_quality],
        "pre_existing_quality_findings": len(quality_findings) - len(introduced_quality),
        "quality_gate_scope": "introduced_only" if baseline_ids is not None else "absolute_no_baseline",
        "format_clean": p8["format_clean"],
        "duplication_percent": p8["duplication_percent"],
        "no_silent_suppression": {"bare_markers": no_silent.bare_markers, "dangling_refs": no_silent.dangling_refs},
    }
    p8["last_gate_report"] = report
    if not passed:
        p8["cycle_count"] = p8["cycle_count"] + 1

    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "p8", "node": "gate_check", **report})
    return {"p8": p8}


async def p8_human_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Cap-hit escalation -- per the plan, P8 never auto-approves past unresolved quality
    findings; a real human decision is required once P8_MAX_CYCLES is exhausted."""
    p8 = state.get("p8") or default_p8_state()
    interrupt({"stage": "p8", "type": "quality_cycle_cap_exceeded", "report": p8.get("last_gate_report")})
    reset = dict(p8)
    reset["cycle_count"] = 0
    return {"p8": reset}
