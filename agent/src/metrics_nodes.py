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
import logging
import os
import re
import shlex
from dataclasses import replace
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from .prompt_loader import load_prompt_pair, render_prompt
from langchain_core.runnables import RunnableConfig

from . import chat_model
from . import config as workflow_config
from . import git_ops, model_config, repo_files, repo_scan, spec_ledger
from .gates.ac_coverage_gate import id_variants
from .gates.test_coverage_gate import MIN_COVERAGE_PERCENT
from .chat_model import get_chat_model_for_thread
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

logger = logging.getLogger(__name__)

METRICS_LATEST_PATH = ".ai-dev-workflow/metrics-latest.json"
TRACEABILITY_MATRIX_PATH = "traceability-matrix.md"

# Regression gate tolerances: a coverage/health movement smaller than this is scan noise (jscpd is
# LOC-sensitive, tool DBs drift), not a regression worth blocking a run over. Health tolerance sits
# below one new medium finding's penalty (3), so a single real new medium still blocks.
MAX_DUPLICATION_PERCENT = float(os.environ.get("MAX_DUPLICATION_PERCENT", "3.0"))
METRIC_REGRESSION_TOLERANCE = float(os.environ.get("METRIC_REGRESSION_TOLERANCE", "1.0"))
HEALTH_REGRESSION_TOLERANCE = float(os.environ.get("HEALTH_REGRESSION_TOLERANCE", "2.0"))
_METRICS_GATE_MAX_ATTEMPTS = 2  # one automatic re-scan for tool flake, then fail



async def _supply_chain_delta(provider: Any, thread_id: str) -> dict[str, Any] | None:
    """Which packages this run added, removed or upgraded -- or None when it cannot be determined.

    Reads the two SBOM snapshots rather than the scan reports: component identity is what syft
    reports reliably, whereas its `dependencies` graph is too sparse on this stack to attribute
    ancestry (see repo_scan.sbom_ancestry). None, never an empty diff, when either side is missing:
    "no baseline SBOM" and "nothing changed" are different facts.
    """
    async def _load(path: str) -> dict[str, Any] | None:
        raw = await repo_files.read_repo_file(provider, thread_id, path)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    return repo_scan.supply_chain_diff(
        await _load(repo_scan.SBOM_BASELINE_PATH), await _load(repo_scan.SBOM_PATH)
    )


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
    """Re-parses whichever coverage artifact the earlier gates already produced -- never re-runs
    coverage a third time, per the plan's explicit instruction.

    Artifacts are SEARCHED FOR, not assumed at two fixed root paths. The old version looked only at
    `TestResults/coverage.cobertura.xml` and `coverage/coverage-summary.json`, which exist only when
    the project sits at the repo root; every generated monorepo writes
    `apps/api.Tests/TestResults/<guid>/coverage.cobertura.xml` instead, so this returned None and the
    regression gate failed the run with "coverage unmeasured" despite a fully measured 100% suite.
    Parsing is delegated to test_coverage_gate's own parsers so both places agree on what a number
    means (including its branch-attribute case handling and per-line condition parsing).
    """
    from .gates.test_coverage_gate import (
        COVERAGE_COMMANDS_PATH,
        _Counts,
        _parse_cobertura_counts,
        _parse_istanbul_counts,
    )

    # The contract written by the coverage gate is the AUTHORITATIVE list: one artifact per test
    # root, naming exactly the file that root's command produces. Globbing the tree instead is
    # actively wrong -- `dotnet test` writes a fresh TestResults/<guid>/ directory on every run, so a
    # repo that has been through a few verify cycles holds a dozen historical snapshots, and summing
    # them counts the same assembly over and over. That produced a confident, entirely fictional
    # "88.9% line / 61.9% branch" for a suite the gate had just measured above 95%.
    contract_paths: list[str] = []
    raw_contract = await repo_files.read_repo_file(provider, thread_id, COVERAGE_COMMANDS_PATH)
    if raw_contract:
        try:
            contract_paths = [
                str(entry["artifact"])
                for entry in (json.loads(raw_contract).get("entries") or [])
                if entry.get("artifact")
            ]
        except (json.JSONDecodeError, TypeError):
            contract_paths = []

    if contract_paths:
        paths = contract_paths
    else:
        # No contract (an older branch, or a stage that never ran): fall back to the NEWEST artifact
        # per test root, so at least nothing is double-counted.
        listing = await provider.exec_in_sandbox(
            thread_id,
            "find . \\( -name 'coverage.cobertura.xml' -o -name 'coverage-summary.json' \\) "
            "-not -path '*/node_modules/*' -not -path './.git/*' -printf '%T@ %p\\n' 2>/dev/null "
            "| sort -rn | head -40",
        )
        newest_per_root: dict[str, str] = {}
        for line in (listing.stdout or "").splitlines():
            _, _, path = line.strip().partition(" ")
            if not path:
                continue
            root = re.split(r"/(?:TestResults|coverage)", path, maxsplit=1)[0]
            newest_per_root.setdefault(root, path)  # sorted newest-first, so the first wins
        paths = list(newest_per_root.values())
    merged: list[_Counts] = []
    for path in paths:
        raw = await repo_files.read_repo_file(provider, thread_id, path.removeprefix("./"))
        if raw is None:
            continue
        counts, _ = (
            _parse_cobertura_counts(raw) if path.endswith(".xml") else _parse_istanbul_counts(raw)
        )
        if counts is not None:
            merged.append(counts)
    if not merged:
        return {"line_rate": None, "branch_rate": None}

    # Counts, not rates, are what merge correctly across stacks: averaging a 10-line worker's rate
    # with a 10k-line app's would weigh them equally.
    lines_covered = sum(c.lines_covered for c in merged)
    lines_total = sum(c.lines_total for c in merged)
    branches_covered = sum(c.branches_covered for c in merged)
    branches_total = sum(c.branches_total for c in merged)
    return {
        "line_rate": (100.0 * lines_covered / lines_total) if lines_total else None,
        # No branch points anywhere is vacuously full coverage, not 0% -- same convention the gate uses.
        "branch_rate": (100.0 * branches_covered / branches_total) if branches_total else 100.0,
    }


def _traceability_rows(
    ac_entries: list[dict[str, Any]], found_tokens: set[str], commit_log: str
) -> list[dict[str, Any]]:
    """Pure half of the matrix build, self-checked in _demo(). Matches each AC's ledger id against
    the id spellings actually found in test files (ac_coverage_gate.id_variants -- the SAME
    tolerance the P4 gate applies, so a test the gate accepted is never invisible here).
    `covered` requires only has_test: every pipeline commit subject is a machine-fixed string that
    never embeds an AC id, so a has_commit requirement made "covered" structurally unreachable
    (observed live: 0/11 covered on a 96%-coverage repo). commits_found stays as an informational
    column."""
    rows: list[dict[str, Any]] = []
    for entry in ac_entries:
        ac_id = entry["id"]
        variants = id_variants(ac_id)
        has_test = bool(found_tokens & set(variants))
        has_commit = any(v in commit_log for v in variants)
        rows.append({
            "us_id": entry.get("parent_us_id", ""),
            "ac_id": ac_id,
            "description": entry.get("description", ""),
            "tests_found": has_test,
            "commits_found": has_commit,
            "status": "covered" if has_test else "untested",
        })
    return rows


async def _build_traceability_matrix(provider: Any, thread_id: str) -> list[dict[str, Any]]:
    entries = await spec_ledger.load_ledger(provider, thread_id)
    ac_entries = [e for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") != "retired"]
    if not ac_entries:
        return []
    log_result = await provider.exec_in_sandbox(thread_id, "git log --oneline -n 500 2>&1")
    commit_log = log_result.stdout or ""

    # Same two-step scan as ac_coverage_gate's fallback: tracked/untracked-but-not-ignored files
    # with test/spec in the path (node_modules etc. are gitignored, so never listed), then ONE
    # grep -F for every id spelling -- no per-file reads, no docker exec per file.
    # The file list is piped into xargs rather than interpolated into the command, and vendored
    # directories are filtered out. Both matter: `git ls-files -co` includes UNTRACKED files, and a
    # run that had npm download a browser into apps/web/.playwright-browsers/ contributed thousands
    # of paths matching /test|spec/, which built a single command line past Windows' 32 KB limit and
    # killed the node outright with "[WinError 206] The filename or extension is too long". xargs
    # chunks the arguments itself, so no file count can reproduce that.
    id_patterns = " ".join(f"-e {shlex.quote(v)}" for e in ac_entries for v in id_variants(e["id"]))
    excluded = "/(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|TestResults|coverage)/"
    grep = await provider.exec_in_sandbox(
        thread_id,
        "git ls-files -co --exclude-standard "
        "| grep -iE '(test|spec)' "
        f"| grep -vE {shlex.quote(excluded)} "
        f"| xargs -r -d '\\n' grep -h -o -F {id_patterns} -- 2>/dev/null | sort -u || true",
    )
    found_tokens = set((grep.stdout or "").split())
    return _traceability_rows(ac_entries, found_tokens, commit_log)


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


def regression_reasons(
    latest_summary: dict[str, Any],
    delta_summ: dict[str, Any] | None,
    coverage: dict[str, Any],
    *,
    baseline_has_findings: bool,
    min_coverage: float | None = None,
    tolerance: float | None = None,
    health_tolerance: float | None = None,
) -> list[str]:
    """Pure decision half of the metrics regression gate (self-checked in _demo). Blocks on:
    open gating findings (severity-floored, introduced-aware -- greenfield's empty-repo baseline
    makes every finding gate absolutely, which is the correct rule there), unmeasured or
    below-threshold coverage, coverage regressing beyond tolerance, and health-score regressing
    beyond tolerance. The health delta is skipped when the baseline has zero findings: greenfield's
    baseline is scanned pre-codegen against an empty repo, and comparing real app code against an
    empty directory is not a regression signal (gating_count covers that case absolutely)."""
    min_cov = MIN_COVERAGE_PERCENT if min_coverage is None else min_coverage
    tol = METRIC_REGRESSION_TOLERANCE if tolerance is None else tolerance
    health_tol = HEALTH_REGRESSION_TOLERANCE if health_tolerance is None else health_tolerance
    reasons: list[str] = []

    gating = latest_summary.get("gating_count") or 0
    if gating > 0:
        reasons.append(f"{gating} gating finding(s) open at/above severity floor {latest_summary.get('severity_floor')!r}")

    line, branch = coverage.get("line_rate"), coverage.get("branch_rate")
    if not isinstance(line, (int, float)) or not isinstance(branch, (int, float)):
        reasons.append("coverage unmeasured -- line/branch rate unavailable, which must never pass as '--%'")
    elif line < min_cov or branch < min_cov:
        reasons.append(f"coverage below threshold: line {line:.1f}%, branch {branch:.1f}% (minimum {min_cov:.0f}%)")

    # Duplication threshold, salvaged from gates/audit_gates.py's verify_audit_exit when that
    # (unreachable) module was deleted. jscpd measures duplication on every full scan and repo_scan
    # reports it, but nothing gated on it once the audit cluster was switched off -- so a run could
    # ship arbitrarily duplicated code with no objection. Absolute threshold, not a delta: a
    # greenfield repo has no baseline to regress against.
    duplication = (latest_summary.get("measures") or {}).get("duplication_percent")
    if isinstance(duplication, (int, float)) and duplication > MAX_DUPLICATION_PERCENT:
        reasons.append(
            f"duplication {duplication:.1f}% exceeds the {MAX_DUPLICATION_PERCENT:.0f}% threshold"
        )

    metric_deltas = (delta_summ or {}).get("metrics") or {}
    for name in ("coverage_line_rate", "coverage_branch_rate"):
        d = metric_deltas.get(name) or {}
        if d.get("direction") == "regressed" and abs(d.get("delta") or 0) > tol:
            reasons.append(f"{name} regressed {d.get('from')} -> {d.get('to')} (beyond {tol}pt tolerance)")
    health = metric_deltas.get("health_score") or {}
    if baseline_has_findings and health.get("direction") == "regressed" and abs(health.get("delta") or 0) > health_tol:
        reasons.append(f"health_score regressed {health.get('from')} -> {health.get('to')} (beyond {health_tol}pt tolerance)")
    return reasons


def make_metrics_route_after_compute():
    """next: gate clean. retry: one automatic re-scan (tool flake). fail: record run_failure and
    continue INTO exit -- never END, so exit.md/manifest/session close still happen and the exit
    verify forces merge_ready=False with these reasons."""

    def route(state: dict[str, Any]) -> str:
        gate = (state.get("repo_scan") or {}).get("metrics_gate") or {}
        if not (gate.get("reasons") or []):
            return "next"
        if int(gate.get("attempt") or 0) < _METRICS_GATE_MAX_ATTEMPTS:
            return "retry"
        return "fail"

    return route


async def metrics_regression_record_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    gate = (state.get("repo_scan") or {}).get("metrics_gate") or {}
    payload = {"stage": "metrics_report", "type": "metrics_regression", "reasons": gate.get("reasons") or []}
    if sandbox_registry.get(thread_id) is not None:
        provider = get_sandbox_provider()
        await repo_files.append_ledger_entry(provider, thread_id, {"node": "regression_gate", **payload})
    # ponytail: no auto-remediation loop from metrics -- the run proceeds into exit with
    # run_failure set and merge blocked; add a fix loop only if this fires frequently.
    return {"run_failure": payload}


async def collect_live_refresh(state: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    """Non-blocking pickup of a finished background refresh scan (kicked by git_ops.commit_all
    after every code-writing commit): merges the fresher summary into repo_scan.latest_summary and
    re-sums the token ledger into token_usage_running, so the metrics bar tracks code churn and
    spend live. Display-only -- the gates' own scans remain the authority, and a refresh landing
    after a gate's scan may briefly show a summary one commit older (the next writer corrects it)."""
    report = repo_scan.pop_finished_refresh(thread_id)
    if report is None or sandbox_registry.get(thread_id) is None:
        return None
    provider = get_sandbox_provider()
    prior_summary = ((state.get("repo_scan") or {}).get("latest_summary")
                     or (state.get("repo_scan") or {}).get("baseline_summary"))
    summary = repo_scan.merge_measures(prior_summary, report.to_dashboard_dict()["summary"], "full")
    prior_repo_scan = dict(state.get("repo_scan") or {})
    prior_repo_scan.update(
        latest_summary=summary,
        latest_duplication_percent=(report.metrics.get("duplication") or {}).get("percent"),
    )
    totals = await _sum_token_usage(provider, thread_id)
    measures = summary.get("measures", {})
    logger.info(
        "repo_scan: background refresh landed thread_id=%s health_score=%s duplication_percent=%s "
        "mean_ccn=%s coverage_line_rate=%s gating_count=%s cost=$%.2f",
        thread_id, summary.get("health_score"), measures.get("duplication_percent"),
        measures.get("mean_ccn"), measures.get("coverage_line_rate"), summary.get("gating_count"),
        totals["total_cost"],
    )
    return {
        "repo_scan": prior_repo_scan,
        "token_usage_running": {
            "input_tokens": totals["total_input_tokens"],
            "output_tokens": totals["total_output_tokens"],
            "cost": totals["total_cost"],
        },
    }


async def metrics_compute_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    run_id = state.get("run_id", "unknown")
    if sandbox_registry.get(thread_id) is None:
        # attempt is forced past the retry budget: without a sandbox a re-scan can't succeed either,
        # so the route must go fail (-> exit, which also no-ops sandbox-less) rather than loop.
        prior_repo_scan = dict(state.get("repo_scan") or {})
        prior_repo_scan["metrics_gate"] = {
            "reasons": ["cannot verify -- sandbox lost before the metrics scan ran"],
            "attempt": _METRICS_GATE_MAX_ATTEMPTS,
        }
        return {"metrics_report": {"metrics": {}}, "repo_scan": prior_repo_scan}

    provider = get_sandbox_provider()
    # The ONE caller that opts into the Eval layer: this is the run's final measurement, so paying
    # for EVAL_ATTEMPTS suite runs buys the per-AC verified/executed/flake numbers the exit report
    # is judged on. Every other caller (the four gate scan points, and the per-commit background
    # refresh) leaves include_eval at its False default and never runs a suite.
    scan = await repo_scan.run_repo_scan(provider, thread_id, profile="full", include_eval=True)
    # Prefer the contract-merged number graph.py's make_verify_node / audit_gates.py's
    # audit_exit_gate_node already promoted onto state.repo_scan.coverage: both read the SAME
    # coverage artifact minimal-code-to-green's own gate produced, whereas _read_coverage_summary
    # re-parses whichever coverage artifact it finds FIRST on disk -- root cobertura wins that
    # search on a dual-stack repo, silently reporting only the .NET half. Re-parse stays as the
    # fallback for a repo/thread that never ran that gate (predates it, or hydrated straight past
    # that stage) -- the delta then compares like-with-like (both contract-merged).
    promoted_coverage = (state.get("repo_scan") or {}).get("coverage") or {}
    if isinstance(promoted_coverage.get("line_rate"), (int, float)) and isinstance(promoted_coverage.get("branch_rate"), (int, float)):
        # Both rates or nothing: the gate's verify_coverage report always carries a numeric branch
        # rate (vacuously 100 when branchless), so a line-only value is stale/partial state -- the
        # 81.8%-branch-in-exit.md incident was a line-only promotion sailing past the branch gate.
        coverage = {"line_rate": promoted_coverage["line_rate"], "branch_rate": promoted_coverage["branch_rate"]}
    else:
        coverage = await _read_coverage_summary(provider, thread_id)
    # Merged in BEFORE the dashboard dict is built (and BEFORE LATEST_PATH is written) so the
    # delta engine's coverage_line_rate metric and the `measures` block both see it.
    scan = replace(scan, metrics={**scan.metrics, "coverage": coverage})
    scan_report = scan.to_dashboard_dict()
    await repo_files.write_repo_file(provider, thread_id, repo_scan.LATEST_PATH, json.dumps(scan_report, indent=2, default=str) + "\n")
    baseline = await _read_baseline(provider, thread_id)
    delta = repo_scan.diff_scans(baseline, scan_report)

    traceability_rows = await _build_traceability_matrix(provider, thread_id)
    token_usage_summary = await _sum_token_usage(provider, thread_id)

    delta_summ = repo_scan.delta_summary(delta)
    attempt = int(((state.get("repo_scan") or {}).get("metrics_gate") or {}).get("attempt") or 0) + 1
    gate_reasons = regression_reasons(
        scan_report["summary"], delta_summ, coverage,
        baseline_has_findings=bool((baseline or {}).get("findings")),
    )

    metrics = {
        "run_id": run_id,
        "repo_scan": scan_report,
        "repo_scan_delta": delta,
        "repo_scan_delta_reason": None if delta else "no baseline recorded for this repository",
        "coverage": coverage,
        "e2e": state.get("e2e"),
        "traceability_summary": {
            "total": len(traceability_rows),
            "covered": sum(1 for r in traceability_rows if r["status"] == "covered"),
            "tests_only": sum(1 for r in traceability_rows if r["status"] == "tests_only"),
            "untested": sum(1 for r in traceability_rows if r["status"] == "untested"),
        },
        "token_usage_summary": token_usage_summary,
        # The Eval layer's two halves, lifted out of the scan report so the exit report and the
        # dashboard do not have to know where inside it they live. `solidly_verified` (linked AND
        # green AND not flaky) is the number this pipeline should be judged on.
        "ac_verification": scan_report.get("ac_verification"),
        "ac_execution": scan_report.get("ac_execution"),
        "supply_chain": await _supply_chain_delta(provider, thread_id),
        # Persisted (not just channel state) so exit's deterministic verify can read the gate's
        # verdict from metrics-latest.json -- deterministic_verify doesn't receive graph state.
        "regression_gate": {"reasons": gate_reasons, "attempt": attempt},
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
    # Deliberately NOT writing `coverage` back here: repo_scan.coverage is the GATES' promotion
    # slot (graph.py's make_verify_node, audit_gates' exit gate). Writing this node's own number
    # into it meant any re-entry "promoted" metrics' prior output as if a gate had verified it.
    prior_repo_scan.update(
        latest_summary=scan_report["summary"],
        latest_duplication_percent=(scan.metrics.get("duplication") or {}).get("percent"),
        delta_summary=delta_summ,
        metrics_gate={"reasons": gate_reasons, "attempt": attempt},
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
        model_name=model_config.get_model_name("metrics-report", "draft", chat_model.PROVIDER),
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


def _demo() -> None:
    """Self-check for the pure traceability matching -- the ledger mints US-####.# ids and tests
    may spell them four ways; every spelling must count, and covered must not require commit ids."""
    entry = {"id": "US-0001.1", "kind": "acceptance_criterion", "status": "active", "parent_us_id": "US-0001", "description": "d"}
    # Every spelling a gate-passing test may use resolves to the same AC.
    for token in ("US-0001.1", "AC-0001.1", "US_0001_1", "AC_0001_1"):
        rows = _traceability_rows([entry], {token}, commit_log="")
        assert rows[0]["status"] == "covered", (token, rows)
    # covered == has_test alone; machine-fixed commit subjects never carry ids.
    rows = _traceability_rows([entry], {"AC-0001.1"}, commit_log="abc123 ai-dev-workflow: metrics_report metrics")
    assert rows[0]["status"] == "covered" and rows[0]["commits_found"] is False
    # No token found -> untested, never tests_only (kept in the summary shape for the frontend).
    rows = _traceability_rows([entry], set(), commit_log="")
    assert rows[0]["status"] == "untested" and rows[0]["tests_found"] is False

    # regression_reasons: the metrics gate's pure decision half.
    clean_summary = {"gating_count": 0, "severity_floor": "medium"}
    good_cov = {"line_rate": 96.0, "branch_rate": 96.0}
    kw = dict(min_coverage=95.0, tolerance=1.0, health_tolerance=2.0)
    # Greenfield: empty-repo baseline, clean scan -> passes (health delta skipped).
    delta = {"metrics": {"health_score": {"from": 100, "to": 94, "delta": -6, "direction": "regressed"}}}
    assert regression_reasons(clean_summary, delta, good_cov, baseline_has_findings=False, **kw) == []
    # Same health regression with a real (non-empty) baseline -> blocks.
    assert any("health_score" in r for r in regression_reasons(clean_summary, delta, good_cov, baseline_has_findings=True, **kw))
    # Gating finding -> blocks.
    assert any("gating" in r for r in regression_reasons({"gating_count": 1, "severity_floor": "medium"}, None, good_cov, baseline_has_findings=False, **kw))
    # Coverage null -> blocks; below threshold -> blocks; the 81.8%-branch incident is caught.
    assert any("unmeasured" in r for r in regression_reasons(clean_summary, None, {"line_rate": None, "branch_rate": None}, baseline_has_findings=True, **kw))
    assert any("below threshold" in r for r in regression_reasons(clean_summary, None, {"line_rate": 96.8, "branch_rate": 81.8}, baseline_has_findings=True, **kw))
    # 99 -> 96 line drop: above the floor but beyond tolerance -> blocks.
    drop = {"metrics": {"coverage_line_rate": {"from": 99.0, "to": 96.0, "delta": -3.0, "direction": "regressed"}}}
    assert any("coverage_line_rate regressed" in r for r in regression_reasons(clean_summary, drop, good_cov, baseline_has_findings=True, **kw))
    # Sub-tolerance wiggle -> passes (scan noise, not a regression).
    wiggle = {"metrics": {"coverage_line_rate": {"from": 96.5, "to": 96.0, "delta": -0.5, "direction": "regressed"}}}
    assert regression_reasons(clean_summary, wiggle, good_cov, baseline_has_findings=True, **kw) == []

    # Route: clean -> next; reasons under the attempt cap -> retry; at cap -> fail.
    route = make_metrics_route_after_compute()
    assert route({"repo_scan": {"metrics_gate": {"reasons": [], "attempt": 1}}}) == "next"
    assert route({"repo_scan": {"metrics_gate": {"reasons": ["x"], "attempt": 1}}}) == "retry"
    assert route({"repo_scan": {"metrics_gate": {"reasons": ["x"], "attempt": 2}}}) == "fail"
    assert route({}) == "next"
    print("metrics_nodes _demo: ok")


if __name__ == "__main__":
    _demo()
