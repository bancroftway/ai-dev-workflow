"""security-remediation -- code security: same bespoke-cluster shape as quality-remediation (agent/src/quality_security/quality_nodes.py),
parameterized differently. Chain: security_scan -> security_triage -> security_ledger_write -> security_fix -> R(security_remediation)
-> security_gate_check -> (loop to security_scan | security_human_gate).

The tool invocations themselves now live in src/repo_scan.py's `security` profile (semgrep, trivy,
gitleaks, and -- new here -- osv-scanner), which runs them fully offline against databases baked
into the sandbox image, normalizes their three different severity models into one, and
deduplicates across them. That dedup is the reason osv-scanner can be added at all: it and trivy
agree on most advisories but name them differently, and without reconciliation the triage step
would be asked to decide the same CVE twice under two names.

Unlike quality-remediation's, this gate is absolute rather than delta-scoped: a vulnerability inherited from the
repository's baseline is still an exploitable vulnerability.

Verification status, stated plainly, same caveat as quality-remediation: NOT exercised against a real sandbox.
Severity normalization is now a real mapping rather than the level-based approximation
severity.py's docstring describes -- repo_scan.py parses trivy's JSON (which carries the vendor's
own CRITICAL/HIGH/... tier) instead of its SARIF, and computes a CVSS v3.1 base score for OSV
findings that carry only a vector.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from ..prompt_loader import load_prompt_pair, render_prompt
from langchain_core.runnables import RunnableConfig

from .. import config as workflow_config
from .. import git_ops, model_config, repo_files, repo_scan
from ..copilot_chat_model import ainvoke_structured, get_chat_model_for_thread
from ..sandbox import registry as sandbox_registry
from ..sandbox.factory import get_sandbox_provider
from .sarif import Finding
from .schemas import TriageResponse
from .severity import meets_or_exceeds
from .suppressions import append_suppression, check_no_silent_suppression

SECURITY_MAX_CYCLES = int(os.environ.get("SECURITY_MAX_CYCLES", "3"))
# The strict gate: zero unsuppressed findings of this severity or above.
#
# Deliberately raised from "low" to "medium", and worth naming as a *relaxation* rather than
# letting it slip in as a side effect: a low floor across semgrep plus two vulnerability databases
# produces a long tail of advisory noise, and a triage step drowning in it is a triage step under
# pressure to rubber-stamp suppressions. Nothing is lost from the dashboard -- low and info
# findings are still collected, deduplicated and reported, they just do not block.
SECURITY_SEVERITY_FLOOR = os.environ.get("SECURITY_SEVERITY_FLOOR", "medium")

TRIVY_SBOM_PATH = ".ai-dev-workflow/sbom.cyclonedx.json"


class SecurityRemediationState(TypedDict):
    cycle_count: int
    findings: list[dict[str, Any]]
    decisions: dict[str, dict[str, Any]]
    baseline_commit: str | None
    sbom_ok: bool
    last_gate_report: dict[str, Any] | None


def default_security_state() -> SecurityRemediationState:
    return {
        "cycle_count": 0,
        "findings": [],
        "decisions": {},
        "baseline_commit": None,
        "sbom_ok": False,
        "last_gate_report": None,
    }


async def security_scan_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    security_remediation = dict(state.get("security_remediation") or default_security_state())

    if sandbox_registry.get(thread_id) is None:
        return {"security_remediation": security_remediation}

    provider = get_sandbox_provider()
    if security_remediation["baseline_commit"] is None:
        head = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD")
        security_remediation["baseline_commit"] = head.stdout.strip() if head.ok else None

    await provider.exec_in_sandbox(thread_id, "mkdir -p agent-work .ai-dev-workflow")
    # include_metrics=False: this is a gate, it wants findings, not a size/churn profile.
    # report_path keeps repo-scan-latest.json fresh for the frontend metrics bar.
    scan = await repo_scan.run_repo_scan(
        provider, thread_id, profile="security", include_metrics=False, report_path=repo_scan.LATEST_PATH
    )
    await git_ops.commit_paths(provider, thread_id, [repo_scan.LATEST_PATH], "ai-dev-workflow: security scan snapshot")
    findings = list(scan.findings)

    # The SBOM is a deliverable, not a finding -- kept here rather than folded into the scanner,
    # and its failure is a hard infra assertion of its own (see security_gate_check_node).
    sbom_result = await provider.exec_in_sandbox(
        thread_id, f"trivy fs --offline-scan --skip-db-update --format cyclonedx -o {TRIVY_SBOM_PATH} . 2>&1"
    )
    security_remediation["sbom_ok"] = sbom_result.ok

    # The CURRENT scan is the only truth about what findings exist. The old merge kept every
    # previously-DECIDED finding in the list forever, so a finding triaged "fix" still counted
    # as unsuppressed after the fix removed it from the code -- the gate could never converge
    # (observed live: floors fixed seven packages and the count went UP). Decisions live in
    # their own dict keyed by stable finding_key: a decision whose finding vanished is inert,
    # and one whose finding persists still applies.
    security_remediation["findings"] = [f.to_dict() for f in findings]

    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "security_remediation", "node": "scan", "finding_count": len(security_remediation["findings"]), "sbom_ok": security_remediation["sbom_ok"]}
    )
    if security_remediation["sbom_ok"]:
        await git_ops.commit_paths(provider, thread_id, [TRIVY_SBOM_PATH], "ai-dev-workflow: security_remediation SBOM")
    # repo_scan is a LastValue channel -- spread prior state (see repo_scan_baseline_node).
    prior_repo_scan = dict(state.get("repo_scan") or {})
    prior_repo_scan["latest_summary"] = scan.to_dashboard_dict()["summary"]
    return {"security_remediation": security_remediation, "repo_scan": prior_repo_scan}


async def security_triage_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    security_remediation = dict(state.get("security_remediation") or default_security_state())

    open_findings = [f for f in security_remediation["findings"] if f["finding_key"] not in security_remediation["decisions"]]
    if not open_findings:
        return {"security_remediation": security_remediation}

    model = get_chat_model_for_thread(
        thread_id,
        "security-remediation",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("security-remediation", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    system, template = load_prompt_pair("security_remediation_triage")
    prompt = render_prompt(template, findings_json=json.dumps(open_findings, indent=2))
    response = await ainvoke_structured(
        model, [SystemMessage(content=system), HumanMessage(content=prompt)], TriageResponse
    )

    decisions = dict(security_remediation["decisions"])
    findings_by_key = {f["finding_key"]: f for f in security_remediation["findings"]}
    for decision in response.decisions:
        finding = findings_by_key.get(decision.finding_key)
        if finding and finding.get("category") == "secret" and decision.decision == "suppress":
            # Never-suppress rule enforced deterministically, not just by prompt instruction --
            # a triage response can't override this by asserting it anyway. Keyed on the category
            # rather than tool=gitleaks: after cross-tool dedup a secret found by both gitleaks and
            # trivy carries whichever tool won the merge, and this rule must not depend on that.
            continue
        decisions[decision.finding_key] = {
            "decision": decision.decision,
            "justification": decision.justification,
            "suppression_marker": decision.suppression_marker,
        }
    security_remediation["decisions"] = decisions
    provider = get_sandbox_provider()
    await repo_files.append_ledger_entry(
        provider, thread_id, {"stage": "security_remediation", "node": "triage", "decision_count": len(response.decisions), "token_usage": model._last_usage}
    )
    return {"security_remediation": security_remediation}


async def security_ledger_write_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    security_remediation = dict(state.get("security_remediation") or default_security_state())
    if sandbox_registry.get(thread_id) is None:
        return {"security_remediation": security_remediation}

    provider = get_sandbox_provider()
    findings_by_key = {f["finding_key"]: f for f in security_remediation["findings"]}
    decisions = dict(security_remediation["decisions"])

    wrote_any = False
    for finding_key, decision in decisions.items():
        if decision["decision"] != "suppress" or "ref" in decision:
            continue
        finding = findings_by_key.get(finding_key)
        if finding is None:
            continue
        finding_obj = Finding(**{k: v for k, v in finding.items() if k != "status"}, status=finding.get("status", "open"))
        ref = await append_suppression(provider, thread_id, "security_remediation", finding_obj, decision["justification"])
        decisions[finding_key] = {**decision, "ref": ref}
        wrote_any = True

    security_remediation["decisions"] = decisions
    # Commit only when something was written -- `git add` on a never-created pathspec is a hard
    # error (observed live in the quality twin of this node).
    if wrote_any:
        await git_ops.commit_paths(provider, thread_id, [".ai-dev-workflow/suppressions.md"], "ai-dev-workflow: security_remediation suppressions")
    return {"security_remediation": security_remediation}


def _version_key(v: str) -> tuple[int, int, int]:
    parts = []
    for token in v.split(".")[:3]:
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def _pick_floor(vulnerable: str, fixed_csv: str) -> str | None:
    """Smallest fixed version in the vulnerable version's own major, else the smallest higher fix.
    Pure -- see the __main__ self-check."""
    fixes = [t.strip() for t in fixed_csv.split(",") if t.strip()]
    if not fixes:
        return None
    vk = _version_key(vulnerable)
    same_major = [f for f in fixes if _version_key(f)[0] == vk[0] and _version_key(f) >= vk]
    if same_major:
        return min(same_major, key=_version_key)
    higher = [f for f in fixes if _version_key(f) > vk]
    return min(higher, key=_version_key) if higher else None


async def _apply_dependency_floors(provider, thread_id: str, findings: list[dict[str, Any]]) -> list[str]:
    """Deterministic pre-remediation: every JS-ecosystem vulnerability finding already names its
    fixed_version, so pinning the floor is mechanical -- exact-version override selectors
    ("name@vulnerable": fixed) in the manager's overrides block, then reinstall. The LLM fixer
    was landing these inconsistently (observed live: one thread converged, the next left
    esbuild@0.18.20 standing through every lap); a known fix version is not judgment work.
    Best-effort by design -- anything left standing still goes to the LLM fixer."""
    # BARE package-name keys, deliberately: pnpm override selectors match the REQUESTED range,
    # not the resolved version, so an exact "sharp@0.34.5" key misses a dependent requesting
    # "^0.33" and the vulnerable resolution survives (observed live -- both versions ended up in
    # the lockfile). A bare key forces every resolution of the package; when several vulnerable
    # majors coexist the highest floor wins. Blast radius is bounded by what follows this node:
    # a breaking bump fails the rebuild/test gates and the LLM fixer adjusts.
    floors: dict[str, str] = {}
    for f in findings:
        package = f.get("package")
        if f.get("category") != "vulnerability" or not isinstance(package, dict):
            continue
        name, version, fixed = package.get("name"), package.get("version"), package.get("fixed_version")
        if not (name and version and fixed):
            continue
        if str(package.get("ecosystem", "")).lower() not in ("npm", "pnpm", "yarn"):
            continue
        choice = _pick_floor(str(version), str(fixed))
        if choice and (name not in floors or _version_key(choice) > _version_key(floors[name])):
            floors[str(name)] = choice
    if not floors:
        return []

    raw = await repo_files.read_repo_file(provider, thread_id, "package.json")
    if raw is None:
        return []
    try:
        pkg = json.loads(raw)
    except json.JSONDecodeError:
        return []
    is_pnpm = await repo_files.read_repo_file(provider, thread_id, "pnpm-lock.yaml") is not None
    overrides = pkg.setdefault("pnpm", {}).setdefault("overrides", {}) if is_pnpm else pkg.setdefault("overrides", {})
    changed = False
    for selector, fixed_version in floors.items():
        if overrides.get(selector) != fixed_version:
            overrides[selector] = fixed_version
            changed = True
    if not changed:
        return []
    await repo_files.write_repo_file(provider, thread_id, "package.json", json.dumps(pkg, indent=2) + "\n")
    install = "pnpm install --no-frozen-lockfile" if is_pnpm else "npm install --no-audit --no-fund"
    result = await provider.exec_in_sandbox(thread_id, f"{install} 2>&1")
    if not result.ok:
        # Roll back a broken floor set rather than leave the tree uninstallable for the LLM lap.
        await provider.exec_in_sandbox(thread_id, "git checkout -- package.json && (pnpm install --no-frozen-lockfile 2>&1 || npm install 2>&1) >/dev/null; true")
        return []
    return sorted(floors)


async def security_fix_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    security_remediation = dict(state.get("security_remediation") or default_security_state())
    if sandbox_registry.get(thread_id) is None:
        return {"security_remediation": security_remediation}

    to_fix = [
        {**f, **security_remediation["decisions"].get(f["finding_key"], {})}
        for f in security_remediation["findings"]
        if security_remediation["decisions"].get(f["finding_key"], {}).get("decision") == "fix"
    ]
    to_suppress = [
        {**f, **security_remediation["decisions"].get(f["finding_key"], {})}
        for f in security_remediation["findings"]
        if security_remediation["decisions"].get(f["finding_key"], {}).get("decision") == "suppress"
    ]

    floors_applied = await _apply_dependency_floors(get_sandbox_provider(), thread_id, to_fix)
    if floors_applied:
        await repo_files.append_ledger_entry(
            get_sandbox_provider(), thread_id,
            {"stage": "security_remediation", "node": "dependency_floors", "applied": floors_applied},
        )

    if to_fix or to_suppress:
        # Own session key (security_remediation-fix:draft), not plan:draft -- sharing it returned plan's cached
        # read-only session so this autopilot fixer silently couldn't write. The fixer uses this
        # STAGE's model (falling back to plan's) -- same reasoning as quality_nodes: fixing is
        # codegen-tier work and must not silently ride plan's roster choice.
        model = get_chat_model_for_thread(
            thread_id,
            "security_remediation-fix",
            "draft",
            github_token=os.environ.get("GITHUB_TOKEN"),
            model_name=model_config.get_model_name("security-remediation", "draft") or model_config.get_model_name("plan", "draft"),
            sandbox=sandbox_registry.get(thread_id),
            agent_mode="autopilot",
        )
        suppress_instructions = [
            f"{s['file']}:{s.get('line')} -- insert exactly: {s['suppression_marker']} -- ref:{s.get('ref', 'MISSING')}"
            for s in to_suppress
        ]
        system, template = load_prompt_pair("security_remediation_fix")
        prompt = render_prompt(
            template,
            to_fix_json=json.dumps(to_fix, indent=2),
            suppress_instructions_json=json.dumps(suppress_instructions, indent=2),
        )
        await model.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        await repo_files.append_ledger_entry(
            get_sandbox_provider(), thread_id, {"stage": "security_remediation", "node": "fix", "token_usage": model._last_usage}
        )
        await git_ops.commit_all(get_sandbox_provider(), thread_id, "ai-dev-workflow: security-remediation code fixes")

    return {"security_remediation": security_remediation}


def make_security_route_after_gate():
    def route(state: dict[str, Any]) -> str:
        security_remediation = state.get("security_remediation") or default_security_state()
        report = security_remediation.get("last_gate_report") or {}
        if report.get("cannot_verify"):
            return "escalate"  # no sandbox -- never loop or pass, a human must see it
        if report.get("passed"):
            return "next"
        if security_remediation["cycle_count"] < SECURITY_MAX_CYCLES:
            return "retry"
        return "escalate"

    return route


async def security_gate_check_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    security_remediation = dict(state.get("security_remediation") or default_security_state())

    if sandbox_registry.get(thread_id) is None:
        # No sandbox means the security gate could not actually run. Escalate rather than pass green.
        security_remediation["last_gate_report"] = {"passed": False, "cannot_verify": True, "reason": "no sandbox -- security gate did not run"}
        return {"security_remediation": security_remediation}
    if security_remediation["baseline_commit"] is None:
        security_remediation["last_gate_report"] = {"passed": True}
        return {"security_remediation": security_remediation}

    provider = get_sandbox_provider()
    no_silent = await check_no_silent_suppression(provider, thread_id, security_remediation["baseline_commit"])

    unsuppressed = [
        f for f in security_remediation["findings"]
        if meets_or_exceeds(f["severity"], SECURITY_SEVERITY_FLOOR)
        and security_remediation["decisions"].get(f["finding_key"], {}).get("decision") != "suppress"
    ]

    # SBOM generation failure is a hard infra assertion, routed to escalation on its own --
    # distinct from a severity-bar failure, per the plan's explicit design.
    passed = security_remediation["sbom_ok"] and not unsuppressed and no_silent.passed
    report = {
        "passed": passed,
        "unsuppressed": [f["finding_key"] for f in unsuppressed],
        "sbom_ok": security_remediation["sbom_ok"],
        "severity_floor": SECURITY_SEVERITY_FLOOR,
        "no_silent_suppression": {"bare_markers": no_silent.bare_markers, "dangling_refs": no_silent.dangling_refs},
    }
    security_remediation["last_gate_report"] = report
    if not passed:
        security_remediation["cycle_count"] = security_remediation["cycle_count"] + 1

    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "security_remediation", "node": "gate_check", **report})
    return {"security_remediation": security_remediation}


async def security_human_gate_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """Cap-hit terminal failure: unresolved security findings END the run with run_failure set
    (no human pause -- spec/plan approval are the only human touchpoints). Counter reset rides in
    the same return so the next resubmission starts fresh."""
    thread_id = config["configurable"]["thread_id"]
    security_remediation = state.get("security_remediation") or default_security_state()
    payload = {"stage": "security_remediation", "type": "security_cycle_cap_exceeded", "report": security_remediation.get("last_gate_report")}
    await git_ops.record_run_failure(thread_id, payload, state.get("run_id"))
    reset = dict(security_remediation)
    reset["cycle_count"] = 0
    return {"security_remediation": reset, "run_failure": payload}
