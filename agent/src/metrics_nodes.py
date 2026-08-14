"""metrics-report -- deterministic metrics + traceability matrix + token tracking. No LLM at all, with one
named exception (ponytail-gain), exactly as the plan specifies.

The tool-running half now lives in src/repo_scan.py, which runs the whole licence-vetted tool set
offline, deduplicates findings across tools, and returns one structured report. metrics-report is where that
report becomes the *final* repo metrics, alongside the delta against the baseline measured at the
top of the graph -- the improvement story, not just the end state.

Verification status: NOT exercised against a real sandbox, same caveat as quality-remediation/security-remediation/audit-cluster/test-hardening. The
scanner's own pure half is self-checked (`uv run python -m src.repo_scan`).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt
from langchain_core.runnables import RunnableConfig

from . import config as workflow_config
from . import git_ops, model_config, repo_files, repo_scan, spec_ledger
from .copilot_chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

METRICS_LATEST_PATH = ".ai-dev-workflow/metrics-latest.json"
TRACEABILITY_MATRIX_PATH = "traceability-matrix.md"

# The AC-id-embedded test-name convention P4's prompt establishes (graph.py's ac_to_tests_draft.md):
# an identifier-safe form for C# (Test_AC_0007_2_...) and a literal [AC-0007.2] string elsewhere.
_AC_ID_IN_TEST_NAME_RE = re.compile(r"Test_AC_(\d{4})_(\d+)_|\[AC-(\d{4})\.(\d+)\]")
_ID_IN_COMMIT_RE = re.compile(r"\b(US-\d{4}|AC-\d{4}\.\d+)\b")


async def _read_baseline(provider: Any, thread_id: str) -> dict[str, Any] | None:
    """The baseline written once at the top of the graph by repo_scan_baseline_node. Absent on a
    repo that ran the pipeline before repo_scan existed -- in which case the delta is omitted with
    a reason, never fabricated as a zero-delta."""
    raw = await repo_files.read_repo_file(provider, thread_id, repo_scan.BASELINE_PATH)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _read_coverage_summary(provider: Any, thread_id: str) -> dict[str, float | None]:
    """Re-parses whichever coverage artifact P6/audit-cluster's own gates already produced -- never
    re-runs coverage a third time, per the plan's explicit instruction."""
    raw_cobertura = await repo_files.read_repo_file(provider, thread_id, "TestResults/coverage.cobertura.xml")
    if raw_cobertura is not None:
        import defusedxml.ElementTree as ET

        try:
            root = ET.fromstring(raw_cobertura)
            return {"line_rate": float(root.get("line-rate", "0")) * 100, "branch_rate": float(root.get("branch-rate", "0")) * 100}
        except Exception:  # noqa: BLE001 -- best-effort metrics, never fail the whole node over a parse error
            pass
    raw_summary = await repo_files.read_repo_file(provider, thread_id, "coverage/coverage-summary.json")
    if raw_summary is not None:
        try:
            total = json.loads(raw_summary).get("total", {})
            return {"line_rate": total.get("lines", {}).get("pct"), "branch_rate": total.get("branches", {}).get("pct")}
        except json.JSONDecodeError:
            pass
    return {"line_rate": None, "branch_rate": None}


async def _build_traceability_matrix(provider: Any, thread_id: str) -> list[dict[str, Any]]:
    entries = await spec_ledger.load_ledger(provider, thread_id)
    log_result = await provider.exec_in_sandbox(thread_id, "git log --oneline -n 500 2>&1")
    commit_log = log_result.stdout or ""
    grep_result = await provider.exec_in_sandbox(thread_id, "grep -rEl 'Test_AC_[0-9]{4}_[0-9]+_|\\[AC-[0-9]{4}\\.[0-9]+\\]' . 2>/dev/null")
    test_files = [line for line in (grep_result.stdout or "").splitlines() if line.strip()]

    ac_ids_in_tests: set[str] = set()
    for path in test_files:
        content = await repo_files.read_repo_file(provider, thread_id, path)
        if content is None:
            continue
        for match in _AC_ID_IN_TEST_NAME_RE.finditer(content):
            groups = match.groups()
            us, ac = (groups[0], groups[1]) if groups[0] else (groups[2], groups[3])
            ac_ids_in_tests.add(f"AC-{us}.{ac}")

    rows: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("kind") != "acceptance_criterion" or entry.get("status") == "retired":
            continue
        ac_id = entry["id"]
        has_test = ac_id in ac_ids_in_tests
        has_commit = ac_id in commit_log or bool(re.search(rf"\b{re.escape(ac_id)}\b", commit_log))
        status = "covered" if has_test and has_commit else ("tests_only" if has_test else "untested")
        rows.append({"us_id": entry.get("parent_us_id", ""), "ac_id": ac_id, "description": entry.get("description", ""), "tests_found": has_test, "commits_found": has_commit, "status": status})
    return rows


def _render_traceability_matrix(rows: list[dict[str, Any]]) -> str:
    lines = ["# Traceability Matrix", "", "Auto-generated by ai-dev-workflow's metrics-report metrics node -- do not hand-edit.", "", "| US | AC | Description | Tests | Commits | Status |", "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['us_id']} | {row['ac_id']} | {row['description'][:60]} | {'yes' if row['tests_found'] else 'no'} | {'yes' if row['commits_found'] else 'no'} | {row['status']} |")
    return "\n".join(lines) + "\n"


async def _sum_token_usage(provider: Any, thread_id: str) -> dict[str, Any]:
    raw = await repo_files.read_repo_file(provider, thread_id, repo_files.LEDGER_PATH)
    totals = {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost": 0.0, "by_stage": {}}
    if raw is None:
        return totals
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = entry.get("token_usage")
        if not usage:
            continue
        stage = entry.get("stage", "unknown")
        by_stage = totals["by_stage"].setdefault(stage, {"input_tokens": 0, "output_tokens": 0, "cost": 0.0})
        for key, total_key in (("input_tokens", "total_input_tokens"), ("output_tokens", "total_output_tokens")):
            value = usage.get(key) or 0
            totals[total_key] += value
            by_stage[key] += value
        cost = usage.get("cost") or 0.0
        totals["total_cost"] += cost
        by_stage["cost"] += cost
    return totals


async def metrics_compute_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    run_id = state.get("run_id", "unknown")
    if sandbox_registry.get(thread_id) is None:
        return {"metrics_report": {"metrics": {}}}

    provider = get_sandbox_provider()
    scan = await repo_scan.run_repo_scan(provider, thread_id, profile="full", report_path=repo_scan.LATEST_PATH)
    scan_report = scan.to_dashboard_dict()
    baseline = await _read_baseline(provider, thread_id)
    delta = repo_scan.diff_scans(baseline, scan_report)

    coverage = await _read_coverage_summary(provider, thread_id)
    traceability_rows = await _build_traceability_matrix(provider, thread_id)
    token_usage_summary = await _sum_token_usage(provider, thread_id)

    metrics = {
        "run_id": run_id,
        "repo_scan": scan_report,
        "repo_scan_delta": delta,
        "repo_scan_delta_reason": None if delta else "no baseline recorded for this repository",
        "coverage": coverage,
        "traceability_summary": {
            "total": len(traceability_rows),
            "covered": sum(1 for r in traceability_rows if r["status"] == "covered"),
            "tests_only": sum(1 for r in traceability_rows if r["status"] == "tests_only"),
            "untested": sum(1 for r in traceability_rows if r["status"] == "untested"),
        },
        "token_usage_summary": token_usage_summary,
    }

    history_path = f".ai-dev-workflow/history/{run_id}-metrics.json"
    await repo_files.write_repo_file(provider, thread_id, history_path, json.dumps(metrics, indent=2, default=str) + "\n")
    await repo_files.write_repo_file(provider, thread_id, METRICS_LATEST_PATH, json.dumps(metrics, indent=2, default=str) + "\n")
    if delta is not None:
        await repo_files.write_repo_file(provider, thread_id, repo_scan.DELTA_PATH, json.dumps(delta, indent=2, default=str) + "\n")
    await repo_files.write_repo_file(provider, thread_id, TRACEABILITY_MATRIX_PATH, _render_traceability_matrix(traceability_rows))
    await repo_files.append_ledger_entry(
        provider, thread_id,
        {"stage": "metrics_report", "node": "metrics", "traceability_summary": metrics["traceability_summary"],
         "health_score": scan_report["summary"]["health_score"], "finding_count": len(scan.findings)},
    )
    await git_ops.commit_paths(
        provider, thread_id,
        [history_path, METRICS_LATEST_PATH, TRACEABILITY_MATRIX_PATH, repo_scan.LATEST_PATH]
        + ([repo_scan.DELTA_PATH] if delta is not None else []),
        "ai-dev-workflow: metrics_report metrics + repo scan + traceability matrix",
    )
    # Curated, small keys for the frontend metrics bar (repo_scan is a LastValue channel -- spread).
    prior_repo_scan = dict(state.get("repo_scan") or {})
    prior_repo_scan.update(
        latest_summary=scan_report["summary"],
        latest_duplication_percent=(scan.metrics.get("duplication") or {}).get("percent"),
        coverage=coverage,
        delta_summary=(delta or {}).get("summary") if isinstance(delta, dict) else None,
    )
    return {"metrics_report": {"metrics": metrics}, "repo_scan": prior_repo_scan}


async def metrics_ponytail_gain_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    """The one LLM call in metrics-report -- explicitly the exception to "no LLM at all," per the plan."""
    thread_id = config["configurable"]["thread_id"]
    metrics_report = dict(state.get("metrics_report") or {"metrics": {}})
    if sandbox_registry.get(thread_id) is None:
        return {"metrics_report": metrics_report}

    model = get_chat_model_for_thread(
        thread_id,
        "metrics-report",
        "draft",
        github_token=os.environ.get("GITHUB_TOKEN"),
        model_name=model_config.get_model_name("metrics-report", "draft"),
        sandbox=sandbox_registry.get(thread_id),
        available_tools=workflow_config.READ_ONLY_AVAILABLE_TOOLS,
    )
    gain_system, gain_human = load_prompt_pair("metrics_report_ponytail_gain")
    response = await model.ainvoke(
        [SystemMessage(content=gain_system), HumanMessage(content=gain_human)]
    )
    provider = get_sandbox_provider()
    metrics = dict(metrics_report.get("metrics") or {})
    metrics["ponytail_benchmark"] = getattr(response, "content", str(response))
    metrics_report["metrics"] = metrics
    await repo_files.write_repo_file(provider, thread_id, METRICS_LATEST_PATH, json.dumps(metrics, indent=2) + "\n")
    await repo_files.append_ledger_entry(provider, thread_id, {"stage": "metrics_report", "node": "ponytail_gain", "token_usage": model._last_usage})
    return {"metrics_report": metrics_report}
