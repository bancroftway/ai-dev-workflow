"""exit -- exit. One LLM judgment node (a StageSpec, ADVERSARIAL/audit pattern reused for
consistency with every other stage, even though the plan's own diagram sketched a single LLM box
-- an adversarial second opinion on "is this merge-ready" is worth having, same reasoning as every
other stage's audit pass) + one deterministic finalization node, exactly as the plan specifies for
the finalize half.

Verification status: NOT exercised against a real sandbox, same caveat as every quality-remediation+ node cluster
this session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
from datetime import datetime, timezone
from typing import Any

from . import approvals, chat_model, git_ops, preflight_nodes, repo_files, repo_scan, session_store, spec_ledger, workflow_persistence
from . import config as workflow_config
from .markdown_render import render_exit_markdown
from .preflight_nodes import MANIFEST_PATH
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# Phrases the metrics regression gate OWNS -- every one of these comes from
# metrics_nodes.regression_reasons and from nowhere else. A blocking reason containing one of them
# is a claim about a deterministic measurement, so this run's gate output is the only authority on
# whether it is true. Anything outside this vocabulary is the drafting model's own reasoning and is
# never second-guessed here. Kept as substrings, not exact strings, because the gate interpolates
# live numbers ("duplication 10.5% exceeds...") that will not match a previous run's text.
_GATE_OWNED_REASON_MARKERS = (
    "gating finding(s) open",
    "coverage unmeasured",
    "coverage below threshold",
    "exceeds the",          # duplication threshold
    "regressed",            # coverage/health regression deltas
    # Any NEW deterministic blocker vocabulary must be added here too, or a blocker fixed on run
    # N re-blocks every later run: the drafting model reads the committed EXIT-REPORT.md and
    # copies old blockers forward verbatim (see verify_exit_readiness's stale-blocker filter).
    # These MUST be phrases only the deterministic checks emit -- a generic substring (an early
    # draft used bare "README.md") deletes the model's own legitimate prose blockers that merely
    # mention the file, and can flip merge_ready back to True on a run that earned its False.
    "README.md is missing or empty",           # readme_gate hard problems (verbatim prefixes)
    "standard-readme requires it",
    "has no H1 title",
    "must be the LAST section",
    "authentication enforcement was required for this run",  # verify_exit_readiness's own blocker
    # exit_finalize_node's run_failure injection. Deliberately NOT "run failed at" -- that exact
    # phrase appears in git_ops's failure commit message (rendered inside the report's own Commits
    # section) and in ordinary model prose ("the smoke-test run failed at login"), so it would both
    # get copied forward and falsely filter legitimate reasons.
    "terminal pipeline failure recorded at",
)

CHANGELOG_PATH = "CHANGELOG.md"
HISTORY_DIR = ".ai-dev-workflow/history"
# Stable, run-id-free location for the LATEST run's exit report, so a human landing on the delivered
# branch can find it without knowing a run id. The per-run copy under HISTORY_DIR remains the archive.
EXIT_REPORT_PATH = ".ai-dev-workflow/EXIT-REPORT.md"

# No retention/pruning of history/ here anymore: that subsystem existed to bound growth across
# MANY sessions dumping artifacts into one shared branch (WS0's single ai-dev-workflow branch).
# Branch-per-session means a branch's history/ dir only ever holds ITS OWN session's attempts
# (one per resume), which is small by construction -- nothing left to prune.


def _hash_content(raw: str | None) -> str | None:
    if raw is None:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _find_prior_ledger_snapshot(provider: Any, thread_id: str, current_run_id: str) -> list[dict[str, Any]] | None:
    result = await provider.exec_in_sandbox(thread_id, "ls .ai-dev-workflow/history/*-ledger-snapshot.json 2>/dev/null")
    paths = [p.strip() for p in (result.stdout or "").splitlines() if p.strip() and current_run_id not in p]
    if not paths:
        return None
    # Lexical sort of history filenames is chronological -- run_id is a hex token, not a sortable
    # timestamp, so this is approximate (relies on files being written in run order, which they
    # are, since each run's snapshot is written once at exit and history/ is never rewritten).
    latest_path = sorted(paths)[-1]
    raw = await repo_files.read_repo_file(provider, thread_id, latest_path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _files_changed(provider: Any, thread_id: str, baseline_commit: str | None) -> tuple[str, str]:
    """git diff --stat and git log --oneline for this run's own commits (baseline..HEAD), as plain
    text blocks for the report artifacts. Empty strings (never None) when there's no baseline to
    diff against -- an old thread predating run_baseline_commit, or a run that never scaffolded."""
    if not baseline_commit:
        return "", ""
    range_arg = f"{baseline_commit}..HEAD"
    diff_result = await provider.exec_in_sandbox(
        thread_id, f"git diff --stat {shlex.quote(range_arg)} -- . {shlex.quote(':!.ai-dev-workflow')}"
    )
    log_result = await provider.exec_in_sandbox(thread_id, f"git log --oneline {shlex.quote(range_arg)}")
    return (diff_result.stdout or "").strip(), (log_result.stdout or "").strip()


async def _list_screenshots(provider: Any, thread_id: str, run_id: str) -> list[str]:
    """Repo-relative paths of whatever's in history/<run_id>-screens/, empty when the dir doesn't
    exist (E2E lands the dir in a later task) -- never fabricated."""
    screens_dir = f"{HISTORY_DIR}/{run_id}-screens"
    result = await provider.exec_in_sandbox(thread_id, f"ls {shlex.quote(screens_dir)} 2>/dev/null")
    names = [n.strip() for n in (result.stdout or "").splitlines() if n.strip()]
    return [f"{screens_dir}/{name}" for name in names]


def _screen_label(filename: str) -> tuple[str, str]:
    """('001-expenses-new.png') -> ('Expenses New', '/expenses/new').

    The route is recovered from the filename because e2e_nodes names each shot after the route it
    captured (_route_slug) -- so the report can say WHICH screen an image shows without a second
    channel of state to keep in sync. Suite-harvested images have no route; they are labelled as
    such rather than given a fabricated one.
    """
    import re

    stem = filename.rsplit(".", 1)[0]
    slug = stem.split("-", 1)[1] if "-" in stem else stem
    # Suite captures now carry the AC id playwright put in its result directory name
    # (`001-US-0005-1-suite.png`), so the report can point an image at the criterion it proves
    # instead of listing anonymous "Test run" rows.
    ac = re.match(r"^(US-\d{4}(?:-\d+)?)-suite$", slug)
    if ac:
        return f"AC {ac.group(1)}", "(from playwright suite)"
    if slug == "suite":
        return "Test run", "(from playwright suite)"
    if slug == "home":
        return "Home", "/"
    return slug.replace("-", " ").title(), "/" + slug.replace("-", "/")


def _render_skills_section(stages: dict[str, Any] | None) -> list[str]:
    """Per-stage skill evidence, from each stage's own session events -- never its self-report.

    Exists because the enforcement was invisible: the gate recorded nothing on the pass path, so a
    green run showed no sign that any methodology skill had been applied. `unverified` marks a stage
    whose session log could not be read, which is deliberately distinct from "no skills required".
    """
    if not stages:
        return []
    rows: list[str] = []
    for stage_key, stage in stages.items():
        skills = (stage or {}).get("skills")
        if not skills:
            continue
        invoked = ", ".join(skills.get("invoked") or []) or "(none)"
        notes: list[str] = []
        if skills.get("missing"):
            notes.append(f"MISSING {', '.join(skills['missing'])}")
        if skills.get("unsubstantiated"):
            notes.append(f"CLAIMED BUT NOT INVOKED {', '.join(skills['unsubstantiated'])}")
        if not skills.get("verified"):
            notes.append("unverified (session log unreadable)")
        rows.append(f"| {stage_key} | {invoked} | {'; '.join(notes) or 'ok'} |")
    if not rows:
        return []
    return ["## Skills invoked per stage", "", "| Stage | Skills invoked | Notes |", "|---|---|---|", *rows, ""]


def _render_eval_section(metrics_summary: dict[str, Any] | None) -> list[str]:
    """Acceptance-criteria verification and execution -- the Eval layer (ac_eval.py).

    Unconditional heading, like the Screens and Skills sections: "we could not measure this" is a
    fact a reviewer needs, and a silently absent section reads as though the question was never
    asked.
    """
    lines = ["## Acceptance criteria: verified and executed", ""]
    verification = (metrics_summary or {}).get("ac_verification") or {}
    execution = (metrics_summary or {}).get("ac_execution") or {}
    if not verification and not execution:
        return lines + ["Not evaluated for this run.", ""]

    if verification.get("status") == "not_evaluated":
        lines.append(f"- **Linked to tests**: not evaluated ({verification.get('reason')})")
    elif verification:
        levels = verification.get("levels") or {}
        unverified = verification.get("unverified") or []
        lines.append(
            f"- **Linked to tests**: {verification.get('linked', 0)}/{verification.get('total', 0)} "
            f"(unit {levels.get('unit', 0)}, integration {levels.get('integration', 0)}, e2e {levels.get('e2e', 0)})"
        )
        if unverified:
            lines.append(f"- **No test names these criteria**: {', '.join(unverified)}")
        if verification.get("e2e_only"):
            lines.append(
                f"- **Proven only at the browser layer**: {', '.join(verification['e2e_only'])} "
                "(no unit or integration test names them)"
            )

    if execution.get("status") == "not_evaluated":
        lines.append(f"- **Execution**: not evaluated ({execution.get('reason')})")
    elif execution:
        lines.append(
            f"- **Solidly verified** (linked AND green AND not flaky): "
            f"**{execution.get('solidly_verified', 0)}** of {verification.get('total', 0)}"
        )
        lines.append(
            f"- **Execution over {execution.get('attempts', 0)} run(s)**: {execution.get('passing', 0)} passing, "
            f"{execution.get('failing', 0)} failing, {execution.get('not_run', 0)} never exercised"
        )
        if execution.get("flaky"):
            lines.append(f"- **Flaky** (passed some runs, failed others): {', '.join(execution['flaky'])}")
    return lines + [""]


def _render_supply_chain_section(metrics_summary: dict[str, Any] | None) -> list[str]:
    """What this run did to the dependency tree."""
    lines = ["## Supply chain", ""]
    chain = (metrics_summary or {}).get("supply_chain")
    if not chain:
        return lines + ["No baseline SBOM recorded for this repository -- nothing to diff.", ""]
    lines.append(
        f"- **Net change**: {chain.get('net_change', 0):+d} components "
        f"({chain.get('added_count', 0)} added, {chain.get('removed_count', 0)} removed)"
    )
    for label, key in (("Added", "added"), ("Removed", "removed"), ("Version changed", "version_changed")):
        items = chain.get(key) or []
        if items:
            shown = ", ".join(f"`{i}`" for i in items[:15])
            more = f" ... and {len(items) - 15} more" if len(items) > 15 else ""
            lines.append(f"- **{label}** ({len(items)}): {shown}{more}")
    return lines + [""]


def _failure_detail(run_failure: dict[str, Any]) -> str:
    """The most specific text a terminal failure recorded, whichever key the escalate site used
    (rebuild: stderr_tail/feedback; stage verify-cap: feedback/report; draft-infra: detail)."""
    for key in ("feedback", "stderr_tail", "detail", "report", "stdout_tail"):
        value = run_failure.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _failure_headline(run_failure: dict[str, Any]) -> str:
    """First non-empty line of the failure detail, single-line, bounded -- for the blocking bullet."""
    detail = _failure_detail(run_failure)
    first = next((line.strip() for line in detail.splitlines() if line.strip()), "")
    return first[:300]


def _render_terminal_failure(run_failure: dict[str, Any] | None) -> str:
    """'## Terminal failure' section: stage, type, failure_type and the recorded output tail
    verbatim in a code block, so the report itself names why the run died."""
    if not run_failure:
        return ""
    lines = [
        "## Terminal failure",
        "",
        f"- **Stage**: {run_failure.get('stage')}",
        f"- **Type**: {run_failure.get('type')} (classified: {run_failure.get('failure_type') or 'unclassified'})",
    ]
    subsequent = run_failure.get("subsequent_failure")
    if subsequent:
        lines.append(f"- **Followed by**: {subsequent.get('stage')}: {subsequent.get('type')}")
    detail = _failure_detail(run_failure)
    if detail:
        lines += ["", "```", detail[-2500:], "```"]
    return "\n".join(lines) + "\n"


def _divergence_ledger(snapshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """(report rows, markdown section) from adversarial-compliance's per-lap divergence snapshots
    (adversarial_gate._snapshot_findings ledger rows, one per verify lap, in lap order).

    Deterministic disposition, no model self-report: a finding is CLOSED when its plan_reference
    stops appearing in the final lap's audit -- the re-audit is the referee for what the fix laps
    actually closed -- and OPEN when the final audit still reports it (with the auditor's own
    proposed_resolution as the "what it would take"). Matched by plan_reference: finding ids are
    per-response placeholders and descriptions get reworded between laps; the Plan step / AC
    reference is the stable anchor. Empty input (pre-feature runs, audit never ran) renders
    nothing."""
    if not snapshots:
        return [], ""
    first_seen: dict[str, int] = {}
    latest: dict[str, dict[str, Any]] = {}
    for lap, snapshot in enumerate(snapshots, start=1):
        for finding in snapshot.get("findings") or []:
            ref = str(finding.get("plan_reference") or "unknown plan reference")
            first_seen.setdefault(ref, lap)
            latest[ref] = finding
    final_refs = {
        str(f.get("plan_reference") or "unknown plan reference")
        for f in (snapshots[-1].get("findings") or [])
    }
    rows = [
        {
            "plan_reference": ref,
            "severity": latest[ref].get("severity"),
            "description": latest[ref].get("description"),
            "status": "open" if ref in final_refs else "closed",
            "first_seen_lap": first_seen[ref],
            "proposed_resolution": latest[ref].get("proposed_resolution") if ref in final_refs else None,
        }
        for ref in sorted(first_seen, key=lambda r: (first_seen[r], r))
    ]
    lines = [
        "## Divergence Ledger (adversarial-compliance)",
        "",
        f"{len(snapshots)} audit lap(s). Dispositions are deterministic: a finding is closed when "
        "the final audit no longer reports it (matched by plan reference), open when it does.",
        "",
    ]
    if not rows:
        lines.append("No divergences were reported on any audit lap.")
    for row in rows:
        if row["status"] == "closed":
            lines.append(
                f"- CLOSED [{row['severity']}] {row['plan_reference']}: {row['description']} "
                f"(first seen lap {row['first_seen_lap']}; absent from the final audit)"
            )
        else:
            lines.append(
                f"- OPEN [{row['severity']}] {row['plan_reference']}: {row['description']} -- "
                f"below the fix threshold; auditor's proposed resolution: "
                f"{row['proposed_resolution'] or '(none given)'}"
            )
    return rows, "\n".join(lines) + "\n"


async def _load_ledger_rows(provider: Any, thread_id: str) -> list[dict[str, Any]]:
    """Every parseable row of this attempt's workflow ledger, in write order (the ledger is reset
    at scaffold on every attempt, resumes included -- so this is one attempt, not the thread)."""
    raw = await repo_files.read_repo_file(provider, thread_id, repo_files.LEDGER_PATH)
    rows: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


async def _load_divergence_snapshots(provider: Any, thread_id: str, run_id: str) -> list[dict[str, Any]]:
    """This run's divergence_snapshot rows from the workflow ledger, in write (lap) order."""
    return [
        entry for entry in await _load_ledger_rows(provider, thread_id)
        if entry.get("node") == "divergence_snapshot" and entry.get("run_id") == run_id
    ]


# Ledger stage keys of the tool-runner sub-reports (stack_runner stage_report rows) that fire
# INSIDE another node's execution. They inherit the stage of the next non-report row, which is the
# node that ran them: rebuild/red-gate inside an r_* placement, coverage-run inside mctg's verify,
# e2e-run inside e2e, test-hardening-run inside test_hardening.
_SUB_REPORT_NODES = frozenset({"stage_report"})


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _stage_summary(
    rows: list[dict[str, Any]],
    stages: dict[str, Any] | None,
    run_failure: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str]:
    """(report rows, markdown section): per-stage runtime, laps, tokens, cost and recorded facts.

    All deterministic, all from THIS attempt's ledger (see _load_ledger_rows): runtime is the sum
    of each row's delta from the previous row (nodes run sequentially, rows are appended at node
    completion, so a row's delta is that node's own wall time); laps are the max of draft-row
    count, verify/rebuild cycle+1 and run-row count; tokens/cost sum token_usage rows (a Copilot
    run reports tokens but cost null -> "n/a"). Notes list only what the ledger and state
    recorded -- gate rejections, fix laps, red-gate blocks, tool-run failures, e2e/test-hardening
    outcomes, the terminal failure -- never a summary the model wrote."""
    if not rows and not stages:
        return [], ""
    ordered = sorted(rows, key=lambda r: r.get("timestamp") or 0)
    # Attribute sub-report rows to the node that ran them (the next non-report row's stage).
    attributed: list[tuple[str, dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    for row in ordered:
        if row.get("node") in _SUB_REPORT_NODES:
            pending.append(row)
            continue
        stage = str(row.get("stage") or "unknown")
        attributed.extend((stage, p) for p in pending)
        pending = []
        attributed.append((stage, row))
    attributed.extend(("unknown", p) for p in pending)

    per: dict[str, dict[str, Any]] = {}
    prev_ts: float | None = None
    for stage, row in attributed:
        entry = per.setdefault(stage, {
            "stage": stage, "runtime_seconds": 0.0, "laps": 0, "input_tokens": 0, "output_tokens": 0,
            "cost": 0.0, "cost_known": False, "notes": [], "_drafts": 0, "_cycle_max": -1, "_runs": 0,
        })
        ts = row.get("timestamp")
        if isinstance(ts, (int, float)):
            if prev_ts is not None and row.get("node") not in _SUB_REPORT_NODES:
                entry["runtime_seconds"] += max(0.0, ts - prev_ts)
            if row.get("node") not in _SUB_REPORT_NODES:
                prev_ts = ts
        node = row.get("node")
        usage = row.get("token_usage") or {}
        if usage:
            entry["input_tokens"] += int(usage.get("input_tokens") or 0)
            entry["output_tokens"] += int(usage.get("output_tokens") or 0)
            if usage.get("cost") is not None:
                entry["cost"] += float(usage["cost"])
                entry["cost_known"] = True
        if node == "draft":
            entry["_drafts"] += 1
        if node in ("verify", "rebuild") and isinstance(row.get("cycle"), int):
            entry["_cycle_max"] = max(entry["_cycle_max"], row["cycle"])
        if node in ("run", "run_tests"):
            entry["_runs"] += 1
        notes = entry["notes"]
        if node == "verify" and row.get("passed") is False:
            notes.append(f"verify rejected lap {row.get('cycle', '?')}")
        if node == "audit":
            if row.get("audit_skipped_infra"):
                notes.append("audit skipped (infra)")
            elif row.get("audit_findings_count"):
                notes.append(f"audit: {row['audit_findings_count']} finding(s)")
        if node == "rebuild":
            if row.get("ok") is False:
                notes.append(f"build/red-gate blocked cycle {row.get('cycle', '?')} ({row.get('verify', 'discovery')})")
            if row.get("red_gate") and str(row["red_gate"]).startswith("TDD-red gate"):
                notes.append("TDD-red gate blocked")
        if node == "stage_report" and row.get("success") is False:
            notes.append(f"tool run failed: {str(row.get('error') or row.get('summary') or '')[:80]}")
        if node == "run":
            notes.append(f"e2e {row.get('status')}: {row.get('passed')}/{row.get('total')} passed (attempt {row.get('attempt')})")
        if node == "run_tests":
            notes.append(f"flaky {row.get('flaky_count')}, stable failures {row.get('stable_fail_count')}")
        if node == "metrics" and row.get("health_score") is not None:
            notes.append(f"health {row['health_score']}, findings {row.get('finding_count')}")
        if node == "readme_write" and row.get("problems"):
            notes.append(f"readme: {len(row['problems'])} problem(s)")
        if node == "run_failure":
            notes.append(f"TERMINAL: {row.get('type')}")
        if node == "divergence_snapshot":
            notes.append(f"audit lap: {len(row.get('findings') or [])} divergence(s)")
    if run_failure and run_failure.get("stage"):
        entry = per.setdefault(str(run_failure["stage"]), {
            "stage": str(run_failure["stage"]), "runtime_seconds": 0.0, "laps": 0, "input_tokens": 0,
            "output_tokens": 0, "cost": 0.0, "cost_known": False, "notes": [], "_drafts": 0, "_cycle_max": -1, "_runs": 0,
        })
        marker = f"TERMINAL: {run_failure.get('type')}"
        if marker not in entry["notes"]:
            entry["notes"].append(marker)
    # Stages the state knows but the ledger never saw: approved-on-resume skips, or never reached.
    for key, stage_state in (stages or {}).items():
        if key in per:
            continue
        status = (stage_state or {}).get("status", "not_started")
        note = "skipped (approved on resume)" if status == "approved" else f"not reached ({status})"
        per[key] = {
            "stage": key, "runtime_seconds": 0.0, "laps": 0, "input_tokens": 0, "output_tokens": 0,
            "cost": 0.0, "cost_known": False, "notes": [note], "_drafts": 0, "_cycle_max": -1, "_runs": 0,
        }

    report_rows: list[dict[str, Any]] = []
    for entry in per.values():
        laps = max(entry["_drafts"], entry["_cycle_max"] + 1, entry["_runs"])
        report_rows.append({
            "stage": entry["stage"],
            "runtime_seconds": round(entry["runtime_seconds"], 1),
            "laps": laps,
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "cost": round(entry["cost"], 4) if entry["cost_known"] else None,
            "notes": entry["notes"],
        })
    lines = [
        "## Stage summary (this attempt)",
        "",
        "Runtime is wall time between ledger rows; laps count draft/verify/fix cycles; notes are "
        "recorded facts only. The ledger resets on every attempt, so a resumed thread's earlier "
        "attempts are not included.",
        "",
        "| Stage | Runtime | Laps | Tokens in/out | Cost | Notes |",
        "|---|---|---|---|---|---|",
    ]
    total_seconds = 0.0
    total_cost = 0.0
    any_cost = False
    for r in report_rows:
        total_seconds += r["runtime_seconds"]
        if r["cost"] is not None:
            total_cost += r["cost"]
            any_cost = True
        cost = f"${r['cost']:.2f}" if r["cost"] is not None else ("n/a" if (r["input_tokens"] or r["output_tokens"]) else "-")
        tokens = f"{r['input_tokens']:,}/{r['output_tokens']:,}" if (r["input_tokens"] or r["output_tokens"]) else "-"
        notes = "; ".join(r["notes"]).replace("|", "\\|") if r["notes"] else "-"
        lines.append(f"| {r['stage']} | {_fmt_duration(r['runtime_seconds'])} | {r['laps'] or '-'} | {tokens} | {cost} | {notes} |")
    lines.append(f"| **Total** | **{_fmt_duration(total_seconds)}** | | | **{'$' + format(total_cost, '.2f') if any_cost else 'n/a'}** | |")
    return report_rows, "\n".join(lines) + "\n"


def _render_history_sections(
    *,
    files_changed_stat: str,
    commits_log: str,
    metrics_summary: dict[str, Any],
    delta_summary: dict[str, Any] | None,
    screenshots: list[str],
    run_id: str,
    e2e: dict[str, Any] | None = None,
    screenshot_prefix: str = "./",
    stages: dict[str, Any] | None = None,
    us_ac_rows: list[dict[str, Any]] | None = None,
    carried_over: list[str] | None = None,
    fallback_metrics: dict[str, Any] | None = None,
) -> str:
    """Deterministic sections appended after render_exit_markdown's own output. Lives here, not in
    markdown_render.py, because that module's contract is content-dict-only (schema-shaped LLM
    output) -- this is free-form derived text (a git diff --stat block, a metrics rollup) with no
    schema behind it."""
    lines: list[str] = ["## What was produced", "", "```", files_changed_stat or "(no baseline recorded for this run -- nothing to diff)", "```", ""]
    lines += ["**Commits this run:**", "", "```", commits_log or "(none)", "```", ""]

    lines += ["## Metrics", ""]
    if metrics_summary:
        coverage = metrics_summary.get("coverage") or {}
        traceability = metrics_summary.get("traceability_summary") or {}
        tokens = metrics_summary.get("token_usage_summary") or {}
        line_rate, branch_rate = coverage.get("line_rate"), coverage.get("branch_rate")
        lines.append(
            f"- **Coverage**: line {line_rate if line_rate is not None else '--'}%, "
            f"branch {branch_rate if branch_rate is not None else '--'}%"
        )
        lines.append(
            f"- **Traceability**: {traceability.get('covered', 0)}/{traceability.get('total', 0)} covered, "
            f"{traceability.get('tests_only', 0)} tests-only, {traceability.get('untested', 0)} untested"
        )
        lines.append(
            f"- **Tokens**: {tokens.get('total_input_tokens', 0)} in / {tokens.get('total_output_tokens', 0)} out "
            f"(${tokens.get('total_cost', 0):.4f})"
        )
    elif fallback_metrics:
        # A run that never reached metrics_compute (every escalate path enters exit directly)
        # still has the per-commit background scan and the token ledger -- degraded, labelled as
        # such, but far better than "Not recorded" on the report a human reads to learn why the
        # run died. Never the final measurement: no coverage merge, no lighthouse, no eval.
        scan = fallback_metrics.get("latest_scan") or {}
        measures = scan.get("measures") or {}
        tokens = fallback_metrics.get("token_usage_summary") or {}
        lines.append("_metrics_compute did not run this attempt -- figures below are the last background scan, not the final measurement._")
        if scan:
            lines.append(
                f"- **Last scan**: health {scan.get('health_score', '--')}, "
                f"duplication {measures.get('duplication_percent', '--')}%, "
                f"mean CCN {measures.get('mean_ccn', '--')}, gating findings {scan.get('gating_count', '--')}"
            )
        if tokens:
            lines.append(
                f"- **Tokens**: {tokens.get('total_input_tokens', 0)} in / {tokens.get('total_output_tokens', 0)} out "
                f"(${tokens.get('total_cost', 0):.4f})"
            )
        if not scan and not tokens:
            lines.append("Not recorded for this run.")
    else:
        lines.append("Not recorded for this run.")
    e2e = e2e or {}
    e2e_status = e2e.get("status") or "not run"
    e2e_line = f"- **E2E**: {e2e_status}"
    if e2e.get("failed_tests"):
        e2e_line += f" ({len(e2e['failed_tests'])}/{e2e.get('total', 0)} failed)"
    elif e2e.get("skipped_reason"):
        e2e_line += f" -- {e2e['skipped_reason']}"
    lines.append(e2e_line)
    lines.append("")

    # Real route names for the Screens table below (e2e_nodes._route_slug is the inverse of the
    # filename e2e wrote); imported lazily -- e2e_nodes is a heavier module than this one needs.
    from .e2e_nodes import _route_slug

    slug_to_route = {_route_slug(r): r for r in (e2e.get("routes") or []) if isinstance(r, str)}

    lighthouse = e2e.get("lighthouse") or {}
    if lighthouse:
        # Lighthouse lives only in report.json today; the report a human reads never showed the
        # per-route scores or the named failing audits (the color-contrast failure in d16959d3
        # was invisible in exit.md while sitting in the JSON).
        lines += ["## Lighthouse (live app, worst-of-routes)", ""]
        lines.append(
            f"- **Performance**: {lighthouse.get('performance', '--')} (floor {workflow_config.LIGHTHOUSE_PERF_MIN or 'report-only'}), "
            f"**Accessibility**: {lighthouse.get('accessibility', '--')} (floor {workflow_config.LIGHTHOUSE_A11Y_MIN or 'report-only'})"
        )
        per_route = lighthouse.get("per_route") or {}
        if per_route:
            lines += ["", "| Route | Performance | Accessibility |", "|---|---|---|"]
            for route, scores in per_route.items():
                lines.append(f"| `{route}` | {(scores or {}).get('performance', '--')} | {(scores or {}).get('accessibility', '--')} |")
        failing = lighthouse.get("failing_audits") or []
        if failing:
            lines += ["", "Failing audits (worst first):", ""]
            for a in failing:
                selector = f" -- `{a['selector']}`" if a.get("selector") else ""
                lines.append(f"- [{a.get('route', '/')}] {a.get('id')}: {a.get('title')} (score {a.get('score')}){selector}")
        lines.append("")

    lines += ["## Delta vs baseline", ""]
    if delta_summary:
        lines += ["| Metric | Before | After | Change |", "|---|---|---|---|"]
        for name, d in (delta_summary.get("metrics") or {}).items():
            lines.append(f"| {name} | {d.get('from')} | {d.get('to')} | {d.get('delta')} ({d.get('direction')}) |")
        lines.append("")
        lines.append(
            f"Findings: {delta_summary.get('fixed_count', 0)} fixed, "
            f"{delta_summary.get('introduced_count', 0)} introduced, "
            f"{delta_summary.get('severity_changed', 0)} severity-changed."
        )
    else:
        lines.append("No baseline recorded for this repository -- nothing to diff.")
    lines.append("")

    # Unconditional sections -- the exit report has a fixed skeleton, and "no screenshots" / "not
    # evaluated" must be stated facts with reasons, never silently missing headings.
    lines += _render_us_ac_section(us_ac_rows, carried_over, run_id)
    lines += _render_eval_section(metrics_summary)
    lines += _render_supply_chain_section(metrics_summary)
    lines += _render_skills_section(stages)

    lines += ["## Screens", ""]
    if screenshots:
        lines += ["| Screen | Route | Screenshot |", "|---|---|---|"]
        for path in screenshots:
            name = path.rsplit("/", 1)[-1]
            screen, route = _screen_label(name)
            # Prefer the real route e2e captured over the filename heuristic: the slug is lossy
            # ("journal-entries" read back as "/journal/entries" in run d16959d3's report).
            stem = name.rsplit(".", 1)[0]
            slug = stem.split("-", 1)[1] if "-" in stem else stem
            if slug in slug_to_route:
                route = slug_to_route[slug]
            lines.append(f"| {screen} | `{route}` | ![{screen}]({screenshot_prefix}{run_id}-screens/{name}) |")
        lines.append("")
        lines.append(f"{len(screenshots)} screenshot(s) captured from the running application.")
        # Which commit the images depict. Stages after e2e (the conformance audit's fix pass) can
        # change UI source, and screenshots then show a tree that no longer exists -- stated here
        # rather than left for a reviewer to discover by comparing pixels to code.
        shot_commit = e2e.get("screenshot_commit")
        if shot_commit:
            lines.append("")
            lines.append(
                f"Captured at commit `{shot_commit}`. If later stages changed UI source, these "
                f"images show that commit and not the tip of the branch."
            )
        blanks = e2e.get("degenerate_screenshots") or []
        if blanks:
            lines.append("")
            lines.append(
                f"**{len(blanks)} capture(s) are too small to contain a rendered page** and are not "
                f"evidence of a working UI: {', '.join(p.rsplit('/', 1)[-1] for p in blanks)}"
            )
        identical = e2e.get("same_size_screenshots") or []
        if identical:
            lines.append("")
            lines.append(
                f"{len(identical)} capture(s) share an exact byte size "
                f"({', '.join(p.rsplit('/', 1)[-1] for p in identical)}) -- usually coincidental PNG "
                f"compression of a similar layout, occasionally a page that never changed. Worth a "
                f"glance, not a blocker."
            )
    else:
        reason = e2e.get("skipped_reason") or ""
        lines.append(f"(none captured -- e2e {e2e_status}{': ' + reason if reason else ''})")
    lines.append("")

    return "\n".join(lines)


def _us_ac_rows(
    entries: list[dict[str, Any]],
    own_us_ids: set[str],
    own_ac_ids: set[str],
    run_id: str,
) -> list[dict[str, Any]]:
    """Per-US/AC provenance rows for this run's exit report. Pure.

    Row set: everything in this run's own approved Specification, plus anything whose derived
    change_status is not "unchanged" (captures retirements, which the spec lists only by id), plus
    anything STAMPED this run (a run can deliver a criterion an earlier run reset -- its change
    column reads "unchanged" but its delivery is this run's news).

    User stories with no acceptance-criterion children are skipped: test-hardening mints synthetic
    "[Flaky test] ..." story entries into the same ledger, and rendering those as requirements
    rows misreports the run. A US row's coded/tested derive from its children (all live children
    stamped -> the latest child stamp), since stamps live only on AC entries.
    """
    ac_children: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if e.get("kind") == "acceptance_criterion" and e.get("parent_us_id"):
            ac_children.setdefault(e["parent_us_id"], []).append(e)

    rows: list[dict[str, Any]] = []
    for e in entries:
        kind = e.get("kind")
        entry_id = e.get("id")
        change = spec_ledger.change_status(e, run_id)
        if kind == "user_story":
            children = ac_children.get(entry_id) or []
            if not children:
                continue
            include = entry_id in own_us_ids or change != "unchanged" or any(
                c.get("coded_run_id") == run_id or c.get("tested_run_id") == run_id for c in children
            )
            if not include:
                continue
            live = [c for c in children if c.get("status") in ("active", "revised")]
            coded = sorted(c.get("coded_run_id") for c in live) if live and all(c.get("coded_run_id") for c in live) else []
            tested = sorted(c.get("tested_run_id") for c in live) if live and all(c.get("tested_run_id") for c in live) else []
            rows.append(
                {
                    "id": entry_id, "kind": kind, "title_or_description": e.get("title", ""),
                    "change": change,
                    "coded_run_id": coded[-1] if coded else None, "coded_at": None,
                    "tested_run_id": tested[-1] if tested else None, "tested_at": None,
                    "test_ids": [],
                }
            )
        elif kind == "acceptance_criterion":
            include = (
                entry_id in own_ac_ids
                or change != "unchanged"
                or e.get("coded_run_id") == run_id
                or e.get("tested_run_id") == run_id
            )
            if not include:
                continue
            rows.append(
                {
                    "id": entry_id, "kind": kind, "title_or_description": e.get("description", ""),
                    "change": change,
                    "coded_run_id": e.get("coded_run_id"), "coded_at": e.get("coded_at"),
                    "tested_run_id": e.get("tested_run_id"), "tested_at": e.get("tested_at"),
                    "test_ids": e.get("test_ids") or [],
                }
            )
    rows.sort(key=lambda r: r["id"])
    return rows


def _undelivered_ac_ids(entries: list[dict[str, Any]]) -> list[str]:
    """Live criteria never delivered by any healthy run, across the WHOLE ledger -- an AC reset by
    a failed run and never re-cited would otherwise be permanently invisible (silent work loss).
    Rendered as the exit report's "carried over" list; the spec ticket-mode prompt tells the next
    ticket to re-cite them."""
    return sorted(
        e["id"]
        for e in entries
        if e.get("kind") == "acceptance_criterion"
        and e.get("status") in ("active", "revised")
        and not e.get("coded_run_id")
    )


def _render_us_ac_section(
    us_ac_rows: list[dict[str, Any]] | None, carried_over: list[str] | None, run_id: str
) -> list[str]:
    """The "which requirements did this run touch/deliver" section -- fixed skeleton, same
    convention as every other exit section."""
    lines = ["## User stories & acceptance criteria this run", ""]
    rows = us_ac_rows or []
    if not rows:
        lines += ["(none recorded -- the specification stage did not run or the ledger is empty)", ""]
    else:
        lines += ["| Id | Change | Title / Description | Coded (run) | Tested (run) | Tests |", "|---|---|---|---|---|---|"]
        for r in rows:
            desc = (r.get("title_or_description") or "").replace("|", "\\|")
            if len(desc) > 90:
                desc = desc[:87] + "..."
            if r.get("kind") == "user_story":
                desc = f"**{desc}**"
            coded = r.get("coded_run_id") or "--"
            if coded != "--" and r.get("coded_run_id") == run_id:
                coded = f"{coded} (this run)"
            tested = r.get("tested_run_id") or "--"
            if tested != "--" and r.get("tested_run_id") == run_id:
                tested = f"{tested} (this run)"
            tests = ", ".join((r.get("test_ids") or [])[:3])
            extra = len(r.get("test_ids") or []) - 3
            if extra > 0:
                tests += f", +{extra} more"
            lines.append(
                f"| {r['id']} | {r.get('change')} | {desc} | {coded} | {tested} | {tests or '--'} |"
            )
        lines.append("")
        coded_not_tested = [
            r["id"] for r in rows
            if r.get("kind") == "acceptance_criterion" and r.get("coded_run_id") and not r.get("tested_run_id")
        ]
        if coded_not_tested:
            lines += [
                f"**Coded but not test-verified**: {', '.join(coded_not_tested)} -- delivered code "
                "whose per-criterion eval never recorded a stable pass.",
                "",
            ]
    if carried_over:
        lines += [
            f"**Carried over -- not delivered**: {', '.join(carried_over)}. These live criteria "
            "have never been delivered by a healthy run; the next ticket's Specification should "
            "re-cite them (unchanged wording) to schedule them.",
            "",
        ]
    return lines


def _diff_ledger(prior: list[dict[str, Any]] | None, current: list[dict[str, Any]]) -> dict[str, list[str]]:
    prior_by_id = {e["id"]: e for e in (prior or [])}
    current_by_id = {e["id"]: e for e in current}
    added = [i for i in current_by_id if i not in prior_by_id]
    retired = [i for i, e in current_by_id.items() if e.get("status") == "retired" and prior_by_id.get(i, {}).get("status") != "retired"]
    revised = [
        i for i, e in current_by_id.items()
        if i in prior_by_id and e.get("last_revised_run_id") != prior_by_id[i].get("last_revised_run_id") and i not in retired
    ]
    return {"added": added, "revised": revised, "retired": retired}


async def verify_exit_readiness(
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: Any,
    _chat_provider: str,
) -> Any:
    """EXIT_SPEC's deterministic_verify: completes the manifest (greenfield re-record + commands --
    only exit has the complete picture, code exists and coverage-commands.json is final), then
    forces merge_ready=False on the merge-readiness draft for any deterministic blocker: the
    metrics regression gate's recorded reasons, a UI app with zero e2e screenshots, or a manifest
    still missing apps/test_command/coverage_commands. Always returns passed=True with the draft
    mutated in place -- an LLM redraft can't fix a code regression or a missing screenshot; the
    downgrade IS the outcome (the no-sandbox cannot_verify path is handled by make_verify_node).

    `_chat_provider` (StageSpec.deterministic_verify's Ruling-4 addition) is unused: this check has
    no chat-model dispatch call of its own."""
    from . import app_discovery  # local: app_discovery imports nothing from exit_nodes, but keep the surface flat
    from .gates.ac_coverage_gate import resolve_test_command
    from .gates.test_coverage_gate import COVERAGE_COMMANDS_PATH
    from .graph import VerificationResult  # local: graph imports exit_nodes (same pattern as audit_gates)
    from .tech_stack_signals import frameworks_have_ui

    def _parse(raw: str | None) -> dict[str, Any]:
        if raw is None:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    manifest = _parse(await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH))
    tech_stack = _parse(await repo_files.read_repo_file(provider, thread_id, workflow_persistence.TECH_STACK_APPROVED_PATH))

    # --- manifest completion: same shape regardless of entrypoint (greenfield or brownfield) ---
    updates: dict[str, Any] = {}
    app_check = manifest.get("app_check") or {}
    if not (app_check.get("apps") or []):
        # app_check_record ran pre-scaffold (empty [] on greenfield, by construction) -- re-scan
        # now that the code exists, exact reuse of e2e_gate_check_node's greenfield re-scan.
        scan = await app_discovery.collect_evidence(provider, thread_id)
        apps = app_discovery.candidates_to_apps(scan.get("candidates") or [])
        if apps:
            updates["app_check"] = {"apps": apps, "evidence_fingerprint": scan.get("fingerprint")}
    if not manifest.get("test_command"):
        command = resolve_test_command(tech_stack)
        if command:
            updates["test_command"] = command
    if not manifest.get("coverage_commands"):
        entries = _parse(await repo_files.read_repo_file(provider, thread_id, COVERAGE_COMMANDS_PATH)).get("entries")
        if entries:
            updates["coverage_commands"] = entries
    if updates:
        manifest = await preflight_nodes.update_manifest(provider, thread_id, updates)

    problems: list[str] = []

    # --- presence: what a merge actually needs recorded ---
    app_check = manifest.get("app_check") or {}
    if app_check.get("suitable") is not False and not (app_check.get("apps") or []):
        problems.append("manifest.json records no runnable app (app_check.apps is empty even after re-scan)")
    if not manifest.get("test_command"):
        problems.append("manifest.json has no test_command for this stack")
    if not manifest.get("coverage_commands"):
        problems.append("manifest.json has no coverage_commands -- coverage is not replayable")

    # --- screenshots: mandatory visual evidence for UI apps, whatever path e2e took (covers all
    # of its skip paths with one check) ---
    is_ui = frameworks_have_ui(tech_stack.get("frameworks") or [])
    screenshots = await _list_screenshots(provider, thread_id, run_id)
    if is_ui and not screenshots:
        problems.append("UI application but no e2e screenshots were captured")

    # --- the metrics regression gate's verdict, run-id-stamped so a stale file never gates ---
    metrics = _parse(await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/metrics-latest.json"))
    if metrics.get("run_id") == run_id:
        problems.extend((metrics.get("regression_gate") or {}).get("reasons") or [])
        # README leg (W7): hard standard-readme problems still open after the leg's own retry
        # laps block the merge -- but only when the leg OWNS the README (a human-authored
        # brownfield README is advisory-only by design, readme_write_node's rule).
        readme = metrics.get("readme") or {}
        if readme.get("owned"):
            problems.extend(readme.get("problems") or [])
    else:
        problems.append("metrics were not recorded for this run -- the regression gate never passed")

    # --- auth enforcement can't silently vanish (W4): a run that REQUIRED auth but whose e2e
    # never ran (non-UI repo, runner missing, suite skipped) verified nothing -- exactly the
    # repos (API-only) where auth matters most. A named blocker, not a silent pass. Read from
    # metrics-latest.json (metrics_compute persists app_auth + the e2e snapshot for exactly this
    # check) -- deterministic verifies are file-based, never graph-state-based.
    if metrics.get("run_id") == run_id:
        app_auth = metrics.get("app_auth") or {}
        e2e_snapshot = metrics.get("e2e") or {}
        auth_required = (
            workflow_config.AIDW_AUTH_GATE
            and app_auth.get("auth_mode") in ("required", "anonymous_list")
            and bool(app_auth.get("secrets_present"))
        )
        # Keyed on the auth gate's own verdict, not e2e.status: an e2e that "passed" without the
        # auth probe ever running (gate exception, posture arriving late on a resumed checkpoint)
        # is just as unverified as a skipped one.
        if auth_required and not (e2e_snapshot.get("auth_check") or {}).get("passed"):
            problems.append(
                "authentication enforcement was required for this run but was not verified "
                f"(e2e status: {e2e_snapshot.get('status') or 'never started'}; auth probe "
                f"{'failed' if e2e_snapshot.get('auth_check') else 'never ran'})"
            )
        elif auth_required:
            # Verified: surface the gate's per-route verdict summary in the exit report (via the
            # report's own risk-notes section) -- the "reported, not blocking" inconclusives
            # otherwise live only in metrics-latest.json.
            auth_note = f"Authentication enforcement verified: {(e2e_snapshot.get('auth_check') or {}).get('feedback')}"
            notes = list(content_dict.get("risk_notes") or [])
            if auth_note not in notes:
                content_dict["risk_notes"] = notes + [auth_note]

    # Drop STALE deterministic blockers the model carried over from a previous run's report.
    #
    # The metrics regression gate owns a fixed vocabulary of reasons, and it is authoritative: if a
    # reason in that vocabulary is not in THIS run's gate output, this run did not have that
    # problem. The drafting model reads the repository, and a previous EXIT-REPORT.md is committed
    # in it -- so it can and does copy old blockers forward verbatim. Observed live (run 45e08f64):
    # regression_gate.reasons was EMPTY, coverage measured 100/100, duplication 0.0%, gating count
    # 0 -- and the report still blocked the merge on "coverage unmeasured", "duplication 10.5%
    # exceeds the 3% threshold" and "1 gating finding(s) open", all three verbatim strings from a
    # previous run. Nothing challenged them, because the check below only ever ADDS blockers.
    #
    # Only gate-owned phrasing is filtered. A prose blocker the model reasoned out for itself (an
    # out-of-scope dependency, a broken replay contract) is exactly what this stage is for and is
    # never touched here.
    gate_reasons = set(problems)
    model_reasons = list(content_dict.get("blocking_reasons") or [])
    kept_reasons, stale_reasons = [], []
    for reason in model_reasons:
        owned = any(marker in reason for marker in _GATE_OWNED_REASON_MARKERS)
        (stale_reasons if owned and reason not in gate_reasons else kept_reasons).append(reason)
    if stale_reasons:
        logger.warning(
            "exit verify: dropping %d blocking reason(s) this run's regression gate did not raise "
            "(carried over from an earlier report): %s",
            len(stale_reasons), "; ".join(r[:120] for r in stale_reasons),
        )
        content_dict["blocking_reasons"] = kept_reasons

    if problems:
        content_dict["merge_ready"] = False
        existing = list(content_dict.get("blocking_reasons") or [])
        content_dict["blocking_reasons"] = existing + [p for p in problems if p not in existing]
        feedback = f"merge_ready forced False: {len(problems)} deterministic blocker(s)"
    elif stale_reasons and not kept_reasons and content_dict.get("merge_ready") is False:
        # Every deterministic check passed AND every blocker the model listed was a stale copy of a
        # gate reason this run did not produce. There is nothing left holding the merge shut, so the
        # False verdict was inherited rather than earned. Left alone, this is precisely the
        # "Ready to merge: False on a clean tree" outcome that sends a human hunting for a defect
        # that was already fixed.
        content_dict["merge_ready"] = True
        logger.warning(
            "exit verify: merge_ready flipped False -> True -- every deterministic check passed and "
            "all %d model-supplied blocker(s) were stale gate reasons from an earlier run",
            len(stale_reasons),
        )
        feedback = "deterministic exit checks passed; cleared stale carried-over blockers"
    else:
        feedback = "deterministic exit checks passed (manifest complete, screenshots present for UI, metrics gate clean)"
    return VerificationResult(
        passed=True,
        feedback=feedback,
        report={"blockers": problems, "ui_app": is_ui, "screenshot_count": len(screenshots), "manifest_completed": sorted(updates)},
    )


def _baseline_refresh_payload(status: str, metrics_summary: dict[str, Any]) -> str | None:
    """The JSON to (over)write `repo_scan.BASELINE_PATH` with on this run's completion, or None to
    leave the baseline untouched.

    Refreshes ONLY on a genuine `completed` status, from THIS SAME run's own final scan_report
    (`metrics_summary["repo_scan"]`, the dashboard dict metrics_compute_node already built and
    checked the regression gate against) -- never on `failed`/`rejected`, so a broken intermediate
    ticket can never become the next ticket's comparison point; the next ticket should still diff
    against the last genuinely completed state. Does not touch `repo_scan_baseline_node`'s own
    idempotency check: nothing writes `BASELINE_PATH` until a ticket actually reaches the
    `completed` branch below, so a still-in-progress ticket's mid-run clarification re-entries --
    which never reach here -- are exactly as protected as they were before this existed.
    """
    if status != "completed":
        return None
    final_scan_report = metrics_summary.get("repo_scan")
    if not final_scan_report:
        return None
    return json.dumps(final_scan_report, indent=2, default=str) + "\n"


async def exit_finalize_node(
    thread_id: str, content: dict[str, Any], state: dict[str, Any], provider: SandboxProvider
) -> None:
    """StageSpec.post_approve_hook for metrics-exit -- the ONLY place this ever runs (never
    add_node'd as a standalone graph node; every run reaches it since requires_human_gate=False
    and verify_exit_readiness always returns passed=True). `content` is the exit stage's own
    approved_content (a MergeReadinessReport dict) -- the caller (_run_post_approve_hook) already
    checked the sandbox is live and content is non-empty before calling.

    A metrics-regression failure is deliberately routed INTO this stage rather than straight to
    git_ops.record_run_failure, specifically so the exit report/changelog/session-close below still
    happen for it -- see the status logic at the bottom, which checks state["run_failure"] first."""
    run_id = state.get("run_id", "unknown")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merge_readiness = content

    terminal_failure = state.get("run_failure")
    if terminal_failure:
        # A terminal escalate (rebuild/e2e/test-hardening) routed into this stage so the report
        # still gets written -- but the drafting model never sees run_failure, so without this the
        # report blames whatever incidental gaps it found ("metrics were not recorded") and never
        # names the actual killer. Injected before update_manifest below so the manifest,
        # report.json, both exit markdowns and the session close all carry it. Phrase is listed in
        # _GATE_OWNED_REASON_MARKERS -- see that tuple's comment.
        # The bullet carries the error's first meaningful line -- the report is the artifact a human
        # reads on the branch, and a bare "rebuild_cap_exceeded" sent the drafting model guessing at
        # a root cause (observed live, run d16959d3: it blamed a missing project reference; the real
        # killer was an MSB4025 XML-comment error that only the DB row and ledger named). The full
        # tail lands in its own section below.
        reason = (
            f"terminal pipeline failure recorded at {terminal_failure.get('stage')}: "
            f"{terminal_failure.get('type')}"
        )
        detail_line = _failure_headline(terminal_failure)
        if detail_line:
            reason = f"{reason} -- {detail_line}"
        existing_reasons = list(merge_readiness.get("blocking_reasons") or [])
        if reason not in existing_reasons:
            merge_readiness["blocking_reasons"] = [reason, *existing_reasons]
        merge_readiness["merge_ready"] = False

    spec_approval = await approvals.latest_approval(provider, thread_id, "specification")
    plan_approval = await approvals.latest_approval(provider, thread_id, "plan")
    raw_requirements = await repo_files.read_repo_file(
        provider, thread_id, workflow_persistence.RAW_REQUIREMENTS_APPROVED_PATH
    )
    raw_metrics = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/metrics-latest.json")
    metrics_summary = json.loads(raw_metrics) if raw_metrics else {}
    if metrics_summary.get("run_id") != run_id:
        # Stale file from a previous run (metrics_compute short-circuited this run) -- rendering
        # it as this run's numbers was the "traceability from a stale manifest" bug. Say "not
        # recorded" instead, and persist nothing stale.
        metrics_summary = {}

    # Read-modify-write, never a wholesale overwrite: manifest.json is co-owned. brownfield-baseline owns
    # `onboarded`, app discovery owns `app_check`, and this node owns the keys below. Overwriting
    # the file (as this node used to) deleted `onboarded` at the end of every run, silently
    # re-triggering brownfield onboarding on the next one.
    await preflight_nodes.update_manifest(
        provider,
        thread_id,
        {
            "run_id": run_id,
            "timestamp": timestamp,
            "requirements_content_hash": _hash_content(raw_requirements),
            "approval_hashes": {
                "specification": spec_approval.content_sha256 if spec_approval else None,
                "plan": plan_approval.content_sha256 if plan_approval else None,
            },
            "metrics_summary": metrics_summary.get("traceability_summary"),
            "merge_readiness": merge_readiness,
        },
    )

    ledger_entries = await spec_ledger.load_ledger(provider, thread_id)
    # US/AC provenance rows: this run's own spec scope from STATE (already in hand -- no sandbox
    # read; the approved file equals it byte-for-byte), row set + carried-over from the ledger.
    own_spec = ((state.get("stages") or {}).get("specification") or {}).get("approved_content") or {}
    own_us_ids = {s.get("id") for s in (own_spec.get("user_stories") or []) if s.get("id")}
    own_ac_ids = spec_ledger.own_ac_ids_from_specification(own_spec)
    us_ac_rows = _us_ac_rows(ledger_entries, own_us_ids, own_ac_ids, run_id)
    carried_over = _undelivered_ac_ids(ledger_entries)
    snapshot_path = f"{HISTORY_DIR}/{run_id}-ledger-snapshot.json"
    prior_snapshot = await _find_prior_ledger_snapshot(provider, thread_id, run_id)
    diff = _diff_ledger(prior_snapshot, ledger_entries)
    await repo_files.write_repo_file(provider, thread_id, snapshot_path, json.dumps(ledger_entries, indent=2) + "\n")

    changelog_section = [f"## {timestamp} (run {run_id})", ""]
    if diff["added"]:
        changelog_section.append(f"- Added: {', '.join(diff['added'])}")
    if diff["revised"]:
        changelog_section.append(f"- Revised: {', '.join(diff['revised'])}")
    if diff["retired"]:
        changelog_section.append(f"- Retired: {', '.join(diff['retired'])}")
    if not any(diff.values()):
        changelog_section.append("- No user-story-level changes since the prior run.")
    changelog_section.append("")

    existing_changelog = await repo_files.read_repo_file(provider, thread_id, CHANGELOG_PATH)
    header = "# Changelog\n\nAuto-generated by ai-dev-workflow's exit exit stage.\n\n"
    body = "\n".join(changelog_section) + "\n"
    if existing_changelog is None:
        new_changelog = header + body
    else:
        # Prepend after the header line(s) -- newest entries first, but keep whatever the
        # existing file's own header/preamble looked like rather than assuming this format wrote
        # it originally.
        new_changelog = existing_changelog.rstrip() + "\n\n" + body

    await repo_files.write_repo_file(provider, thread_id, CHANGELOG_PATH, new_changelog)

    # Status logic: merge_ready-aware, not just run_failure-aware -- a run that reaches exit but
    # fails a DETERMINISTIC gate (verify_exit_readiness forcing merge_ready=False: missing
    # screenshots, no test command, a metrics regression) must be recorded "failed" and stay
    # resumable, exactly like a hard crash. Getting this wrong would make an actually-unsuccessful
    # session permanently unresumable once resume is server-enforced against status=="completed".
    run_failure = state.get("run_failure")
    merge_ready = bool(merge_readiness.get("merge_ready")) if merge_readiness else False
    if run_failure:
        status = "failed"
        failure_payload = run_failure
    elif merge_ready:
        status = "completed"
        failure_payload = None
    else:
        status = "failed"
        failure_payload = {
            "stage": "exit",
            "type": "gates_not_passed",
            "feedback": "; ".join((merge_readiness or {}).get("blocking_reasons") or []) or "exit gates did not pass",
        }

    # Ruling 8, Part B: refresh the regression baseline from this run's own final scan -- only on
    # the completed branch above, see _baseline_refresh_payload's own docstring for why.
    baseline_payload = _baseline_refresh_payload(status, metrics_summary)
    if baseline_payload is not None:
        await repo_files.write_repo_file(provider, thread_id, repo_scan.BASELINE_PATH, baseline_payload)

    pr_url = None
    if status == "completed":
        session_row = await session_store.get_session(thread_id)
        if session_row and session_row.get("pr_url"):
            # Idempotency: the hydrate-short-circuit path can re-fire this hook for an already-
            # approved exit stage (e.g. a resumed thread) -- never open a second PR for one session.
            pr_url = session_row["pr_url"]
        elif session_row:
            token = git_ops.get_push_token(thread_id)
            if token:
                pr_url = await git_ops.open_pull_request(
                    owner=session_row["owner"],
                    repo=session_row["repo"],
                    source_branch=session_row["source_branch"],
                    work_branch=session_row["work_branch"],
                    title=merge_readiness.get("pr_title") or f"ai-dev-workflow: {run_id}",
                    body=merge_readiness.get("pr_description_markdown") or "",
                    token=token,
                )
            else:
                logger.warning("no push token retained for thread_id=%s -- skipping PR creation", thread_id)

    await session_store.close_session(
        thread_id,
        run_id=run_id,
        status=status,
        failure=failure_payload,
        merge_ready=merge_ready if merge_readiness else None,
        pr_title=(merge_readiness or {}).get("pr_title"),
        pr_url=pr_url,
    )

    # Per-run exit report artifacts (durable even once the session ages out of the UI's recent
    # list): the raw
    # diff/log this run actually produced, plus the same metrics/delta numbers the frontend Report
    # tab shows live, frozen at exit time so a past session's report page can render identically.
    files_changed_stat, commits_log = await _files_changed(provider, thread_id, state.get("run_baseline_commit"))
    screenshots = await _list_screenshots(provider, thread_id, run_id)
    delta_summary = repo_scan.delta_summary(metrics_summary.get("repo_scan_delta"))
    ledger_rows = await _load_ledger_rows(provider, thread_id)
    divergence_rows, divergence_section = _divergence_ledger(
        [r for r in ledger_rows if r.get("node") == "divergence_snapshot" and r.get("run_id") == run_id]
    )
    stage_rows, stage_section = _stage_summary(ledger_rows, state.get("stages"), terminal_failure)
    fallback_metrics: dict[str, Any] | None = None
    if not metrics_summary:
        # Escalated runs skip metrics_compute; surface what already exists instead of nothing.
        from . import metrics_nodes

        latest_scan = (state.get("repo_scan") or {}).get("latest_summary") or (state.get("repo_scan") or {}).get("baseline_summary")
        try:
            token_totals = await metrics_nodes._sum_token_usage(provider, thread_id)  # noqa: SLF001 -- same package
        except Exception:  # noqa: BLE001 -- ledger read is best-effort here
            token_totals = None
        if latest_scan or token_totals:
            fallback_metrics = {"latest_scan": latest_scan, "token_usage_summary": token_totals}

    report_path = f"{HISTORY_DIR}/{run_id}-report.json"
    exit_md_path = f"{HISTORY_DIR}/{run_id}-exit.md"

    report_payload = {
        "run_id": run_id,
        "timestamp": timestamp,
        "merge_readiness": merge_readiness,
        "metrics": metrics_summary,
        # Not in the plan's literal artifact shape, but required to render "Delta vs baseline" on
        # a past-session report page without re-deriving it from metrics_summary's raw repo_scan_delta
        # diff (that transform, repo_scan.delta_summary, is Python-only) -- cheap to persist since
        # it's already computed for the exit.md section below.
        "delta_summary": delta_summary,
        "files_changed": files_changed_stat,
        "commits": commits_log,
        "e2e": state.get("e2e"),
        "screenshots": screenshots,
        # Machine-readable US/AC provenance for this run -- same rows the markdown section renders.
        "us_ac": us_ac_rows,
        "carried_over_ac_ids": carried_over,
        # Machine-readable divergence dispositions -- same rows the Divergence Ledger section renders.
        "divergence_ledger": divergence_rows,
        # The terminal failure verbatim (None on a run that reached exit normally) -- the report
        # page and the support-issue body read this, not the prose blockers.
        "run_failure": terminal_failure,
        # Per-stage runtime/laps/tokens/cost/notes -- same rows the Stage summary section renders.
        "stage_summary": stage_rows,
    }
    failure_section = _render_terminal_failure(terminal_failure)
    if stage_section:
        failure_section = stage_section + ("\n" + failure_section if failure_section else "")
    await repo_files.write_repo_file(provider, thread_id, report_path, json.dumps(report_payload, indent=2, default=str) + "\n")

    exit_markdown = render_exit_markdown(merge_readiness or {}) + "\n" + _render_history_sections(
        files_changed_stat=files_changed_stat,
        commits_log=commits_log,
        metrics_summary=metrics_summary,
        delta_summary=delta_summary,
        screenshots=screenshots,
        run_id=run_id,
        e2e=state.get("e2e"),
        stages=state.get("stages"),
        us_ac_rows=us_ac_rows,
        carried_over=carried_over,
        fallback_metrics=fallback_metrics,
    ) + ("\n" + failure_section if failure_section else "") + ("\n" + divergence_section if divergence_section else "")
    await repo_files.write_repo_file(provider, thread_id, exit_md_path, exit_markdown)

    # A second copy at a FIXED, obvious path. The per-run file above is the archive, but its name
    # carries a run id and sits a directory deep, so on a delivered branch nobody finds it -- the
    # report was reviewed as "missing" for five consecutive runs while being committed every time.
    # Screenshot links are re-based to history/... because this copy lives one level up from them.
    latest_markdown = render_exit_markdown(merge_readiness or {}) + "\n" + _render_history_sections(
        files_changed_stat=files_changed_stat,
        commits_log=commits_log,
        metrics_summary=metrics_summary,
        delta_summary=delta_summary,
        screenshots=screenshots,
        run_id=run_id,
        e2e=state.get("e2e"),
        screenshot_prefix="history/",
        stages=state.get("stages"),
        us_ac_rows=us_ac_rows,
        carried_over=carried_over,
        fallback_metrics=fallback_metrics,
    ) + ("\n" + failure_section if failure_section else "") + ("\n" + divergence_section if divergence_section else "")
    await repo_files.write_repo_file(provider, thread_id, EXIT_REPORT_PATH, latest_markdown)

    commit_targets = [MANIFEST_PATH, HISTORY_DIR, CHANGELOG_PATH, EXIT_REPORT_PATH]
    if baseline_payload is not None:
        commit_targets.append(repo_scan.BASELINE_PATH)
    await git_ops.commit_paths(
        provider,
        thread_id,
        commit_targets,
        "ai-dev-workflow: exit finalize (manifest, changelog, exit report)",
    )

    # Graceful end-of-run release of this thread's ~20 Copilot sessions. metrics-exit is genuinely
    # the last stage -- every other terminal path (metrics regression, test-hardening, e2e escalate,
    # and the four rebuild escalates on their sandbox-alive branch) routes INTO metrics-exit_draft
    # rather than END -- so nothing downstream needs a session. run_headless.py already did this at
    # process exit; the server path never did, which left every completed run's sessions riding
    # until the sandbox idle-reaper eventually took the container down.
    # Deliberately NOT done on the needs_clarification -> END path: there the user is about to
    # answer the model's own question, and that stage's conversation continuity is wanted.
    await chat_model.close_thread_session(thread_id, provider=state["provider"])


def _demo() -> None:
    """Self-check for this module's pure halves: `cd agent && uv run python -m src.exit_nodes`."""
    # _diff_ledger: added/revised/retired classification against a prior snapshot.
    prior = [{"id": "US-0001", "status": "active", "last_revised_run_id": "r1"}]
    current = [
        {"id": "US-0001", "status": "active", "last_revised_run_id": "r2"},
        {"id": "US-0002", "status": "retired", "last_revised_run_id": "r2"},
    ]
    diff = _diff_ledger(prior, current)
    assert diff == {"added": ["US-0002"], "revised": ["US-0001"], "retired": ["US-0002"]}, diff
    assert _diff_ledger(None, current)["added"] == ["US-0001", "US-0002"]

    # _baseline_refresh_payload (Ruling 8, Part B): refreshes ONLY on a genuine `completed` status,
    # from that same run's own final scan_report -- never on `failed`, and never fabricated when
    # metrics never recorded one (an old thread, or a run that died before metrics ran).
    final_scan = {"summary": {"gating_count": 0}, "findings": []}
    assert _baseline_refresh_payload("completed", {"repo_scan": final_scan}) == json.dumps(final_scan, indent=2, default=str) + "\n"
    assert _baseline_refresh_payload("failed", {"repo_scan": final_scan}) is None, "a failed run must never refresh the baseline"
    assert _baseline_refresh_payload("completed", {}) is None, "no recorded scan -- nothing to write"
    assert _baseline_refresh_payload("completed", {"repo_scan": None}) is None

    # Screens table uses the route e2e actually captured; Lighthouse section renders scores +
    # named failing audits (previously only in report.json).
    routed = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[".ai-dev-workflow/history/r1-screens/002-journal-entries.png"], run_id="r1",
        e2e={
            "status": "passed", "routes": ["/accounts", "/journal-entries"],
            "lighthouse": {
                "performance": 54, "accessibility": 93,
                "per_route": {"/accounts": {"performance": 55, "accessibility": 93}},
                "failing_audits": [{"id": "color-contrast", "title": "Insufficient contrast", "score": 0, "selector": "button.btn", "route": "/accounts"}],
            },
        },
    )
    assert "| Journal Entries | `/journal-entries` |" in routed, routed
    assert "## Lighthouse" in routed and "color-contrast" in routed and "`button.btn`" in routed and "| `/accounts` | 55 | 93 |" in routed, routed

    # Fallback metrics on a run that never reached metrics_compute: the last background scan and
    # the token ledger, explicitly labelled as not the final measurement.
    degraded = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None, screenshots=[], run_id="r1",
        e2e=None,
        fallback_metrics={
            "latest_scan": {"health_score": 22, "gating_count": 0, "measures": {"duplication_percent": 0.0, "mean_ccn": 1.2}},
            "token_usage_summary": {"total_input_tokens": 10, "total_output_tokens": 20, "total_cost": 4.17},
        },
    )
    assert "not the final measurement" in degraded and "health 22" in degraded and "$4.1700" in degraded, degraded
    assert "Not recorded for this run." not in degraded.split("## Delta")[0]

    # _stage_summary: runtime = deltas between consecutive rows (sub-reports attributed to the node
    # that ran them), laps from cycles/drafts, tokens+cost summed per stage, notes = recorded facts.
    t0 = 1_000_000.0
    ledger = [
        {"timestamp": t0, "stage": "scaffold", "node": "scaffold", "action": "x"},
        {"timestamp": t0 + 60, "stage": "specification", "node": "draft", "readiness": True,
         "token_usage": {"model": "sonnet", "input_tokens": 100, "output_tokens": 50, "cost": 0.5}},
        {"timestamp": t0 + 70, "stage": "specification", "node": "verify", "passed": False, "cycle": 0},
        {"timestamp": t0 + 130, "stage": "specification", "node": "draft", "readiness": True,
         "token_usage": {"model": "sonnet", "input_tokens": 100, "output_tokens": 50, "cost": 0.5}},
        {"timestamp": t0 + 140, "stage": "specification", "node": "verify", "passed": True, "cycle": 1},
        {"timestamp": t0 + 200, "stage": "rebuild", "node": "stage_report", "success": False, "error": "MSB4025 boom"},
        {"timestamp": t0 + 210, "stage": "r_ac_to_tests", "node": "rebuild", "ok": False, "cycle": 0, "verify": "discovery"},
        {"timestamp": t0 + 300, "stage": "r_ac_to_tests", "node": "rebuild", "ok": True, "cycle": 1, "verify": "replay"},
        {"timestamp": t0 + 360, "stage": "e2e", "node": "run", "status": "passed", "passed": 3, "total": 3, "attempt": 1},
    ]
    stage_rows, stage_md = _stage_summary(
        ledger, {"plan": {"status": "approved"}, "remediation": {"status": "not_started"}}, None
    )
    by_stage = {r["stage"]: r for r in stage_rows}
    spec = by_stage["specification"]
    assert spec["runtime_seconds"] == 140.0 and spec["laps"] == 2 and spec["cost"] == 1.0, spec
    assert spec["input_tokens"] == 200 and "verify rejected lap 0" in spec["notes"], spec
    rb = by_stage["r_ac_to_tests"]
    assert rb["runtime_seconds"] == 160.0 and rb["laps"] == 2, rb  # stage_report row folded in
    assert any("tool run failed: MSB4025" in n for n in rb["notes"]) and any("replay" not in n and "discovery" in n for n in rb["notes"]), rb
    assert by_stage["plan"]["notes"] == ["skipped (approved on resume)"], by_stage["plan"]
    assert by_stage["remediation"]["notes"] == ["not reached (not_started)"]
    assert "e2e passed: 3/3 passed" in by_stage["e2e"]["notes"][0]
    assert "| specification | 2:20 | 2 | 200/100 | $1.00 |" in stage_md, stage_md
    assert "**Total**" in stage_md
    _, failed_md = _stage_summary(ledger, {}, {"stage": "r_ac_to_tests", "type": "rebuild_cap_exceeded"})
    assert "TERMINAL: rebuild_cap_exceeded" in failed_md
    assert _stage_summary([], None, None) == ([], "")

    # Terminal-failure rendering: the bullet headline is the error's first line; the section carries
    # the tail verbatim. A report without the real error sent the drafting model guessing (d16959d3).
    rf = {
        "stage": "r_ac_to_tests", "type": "rebuild_cap_exceeded", "failure_type": "gate_exhausted",
        "stdout_tail": "", "feedback": "apps/api.Tests/Api.Tests.csproj(23,67): error MSB4025: bad XML comment\n\nBuild FAILED.",
    }
    assert _failure_headline(rf).startswith("apps/api.Tests/Api.Tests.csproj(23,67): error MSB4025"), _failure_headline(rf)
    section = _render_terminal_failure(rf)
    assert "## Terminal failure" in section and "MSB4025" in section and "rebuild_cap_exceeded" in section, section
    assert _render_terminal_failure(None) == ""
    assert _failure_headline({"stage": "x", "type": "y"}) == ""

    # _divergence_ledger: deterministic dispositions from lap snapshots -- closed = absent from the
    # final lap (matched by plan_reference), open = still reported, first_seen tracked across laps.
    snaps = [
        {"findings": [
            {"severity": "minor", "plan_reference": "Plan Step 4", "description": "copy drift", "proposed_resolution": "align"},
            {"severity": "minor", "plan_reference": "US-0002.1", "description": "missing aria label", "proposed_resolution": "add label"},
        ]},
        {"findings": [
            {"severity": "minor", "plan_reference": "US-0002.1", "description": "aria label still missing", "proposed_resolution": "add the label"},
        ]},
    ]
    rows, section = _divergence_ledger(snaps)
    by_ref = {r["plan_reference"]: r for r in rows}
    assert by_ref["Plan Step 4"]["status"] == "closed" and by_ref["US-0002.1"]["status"] == "open", rows
    assert by_ref["US-0002.1"]["proposed_resolution"] == "add the label", rows
    assert "CLOSED [minor] Plan Step 4" in section and "OPEN [minor] US-0002.1" in section, section
    assert _divergence_ledger([]) == ([], "")
    zero_rows, zero_section = _divergence_ledger([{"findings": []}])
    assert zero_rows == [] and "No divergences" in zero_section, zero_section

    # _render_history_sections: "not recorded"/"no baseline" placeholders when data is absent,
    # real content when present, and a FIXED skeleton -- the screenshots section always renders,
    # stating why it's empty (e2e status + skip reason) rather than silently missing.
    empty = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None, screenshots=[], run_id="r1",
        e2e={"status": "skipped", "skipped_reason": "no UI framework"},
    )
    assert "not recorded for this run" in empty.lower()
    assert "no baseline recorded" in empty.lower()
    assert "## Screens" in empty
    assert "(none captured -- e2e skipped: no UI framework)" in empty
    assert "- **E2E**: skipped -- no UI framework" in empty

    filled = _render_history_sections(
        files_changed_stat="1 file changed",
        commits_log="abc123 do the thing",
        metrics_summary={"coverage": {"line_rate": 80.0, "branch_rate": 70.0}, "traceability_summary": {"total": 2, "covered": 1, "tests_only": 1, "untested": 0}, "token_usage_summary": {"total_input_tokens": 100, "total_output_tokens": 50, "total_cost": 0.01}},
        delta_summary={"fixed_count": 1, "introduced_count": 0, "severity_changed": 0, "metrics": {"coverage_line_rate": {"from": 70, "to": 80, "delta": 10, "direction": "improved"}}},
        screenshots=[
            ".ai-dev-workflow/history/r1-screens/001-home.png",
            ".ai-dev-workflow/history/r1-screens/002-expenses-new.png",
            ".ai-dev-workflow/history/r1-screens/003-suite.png",
        ],
        run_id="r1",
        e2e={"status": "passed", "total": 3, "passed": 3, "failed_tests": []},
    )
    assert "1 file changed" in filled
    assert "80.0%" in filled
    assert "coverage_line_rate" in filled
    assert "- **E2E**: passed" in filled
    assert "## Screens" in filled and "./r1-screens/001-home.png" in filled
    assert "(none captured" not in filled
    # Each screenshot is LABELLED with the screen and route it shows -- "list of screens created"
    # is the point of the section, not an unlabelled pile of images.
    assert "| Home | `/` |" in filled, filled
    assert "| Expenses New | `/expenses/new` |" in filled, filled
    assert "| Test run | `(from playwright suite)` |" in filled, filled

    # The stable-pointer copy lives one directory above the images, so its links must be re-based;
    # a "./" prefix there would 404 for every screenshot.
    rebased = _render_history_sections(
        files_changed_stat="x", commits_log="y", metrics_summary={}, delta_summary=None,
        screenshots=[".ai-dev-workflow/history/r1-screens/001-home.png"], run_id="r1",
        e2e={"status": "passed"}, screenshot_prefix="history/",
    )
    assert "(history/r1-screens/001-home.png)" in rebased, rebased

    # _screen_label: filename -> (screen, route). Route is recovered from the name e2e wrote.
    assert _screen_label("001-home.png") == ("Home", "/")
    assert _screen_label("002-expenses.png") == ("Expenses", "/expenses")
    assert _screen_label("003-expenses-new.png") == ("Expenses New", "/expenses/new")
    assert _screen_label("004-suite.png")[1] == "(from playwright suite)"
    # AC-tagged suite captures are labelled with the criterion they prove.
    assert _screen_label("001-US-0005-1-suite.png") == ("AC US-0005-1", "(from playwright suite)")
    assert _screen_label("002-US-0002-suite.png") == ("AC US-0002", "(from playwright suite)")

    # Skills section: evidence per stage, with a claimed-but-never-invoked skill called out. Empty
    # when no stage recorded any, so the section never appears as an empty heading.
    assert _render_skills_section(None) == []
    assert _render_skills_section({"plan": {}}) == []
    _rows = _render_skills_section({
        "plan": {"skills": {"invoked": ["writing-plans"], "missing": [], "unsubstantiated": [], "verified": True}},
        "ac-to-tests": {"skills": {"invoked": ["ac-to-tests"], "missing": ["test-driven-development"],
                                    "unsubstantiated": ["test-driven-development"], "verified": True}},
        "plan-b": {"skills": {"invoked": [], "missing": [], "unsubstantiated": [], "verified": False}},
    })
    _text = "\n".join(_rows)
    assert "| plan | writing-plans | ok |" in _text
    assert "MISSING test-driven-development" in _text
    assert "CLAIMED BUT NOT INVOKED" in _text, _text
    assert "unverified (session log unreadable)" in _text

    # _us_ac_rows / _undelivered_ac_ids / _render_us_ac_section: US/AC provenance in the exit
    # report -- own-spec scope + changed entries + delivered-this-run entries, flake-ticket
    # synthetic stories filtered, US coded/tested aggregated from children.
    ledger = [
        {"id": "US-0001", "kind": "user_story", "status": "active", "title": "Counter",
         "first_seen_run_id": "r1", "last_revised_run_id": "r1"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "parent_us_id": "US-0001",
         "status": "active", "description": "Increments", "first_seen_run_id": "r1",
         "last_revised_run_id": "r1", "coded_run_id": "r1", "coded_at": "t1",
         "tested_run_id": "r1", "tested_at": "t1", "test_ids": ["[US-0001.1] increments"]},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "parent_us_id": "US-0001",
         "status": "revised", "description": "Shows doubled value", "first_seen_run_id": "r1",
         "last_revised_run_id": "r2", "coded_run_id": "r2", "coded_at": "t2",
         "tested_run_id": "r2", "tested_at": "t2", "test_ids": ["[US-0001.2] doubles"]},
        {"id": "US-0002", "kind": "user_story", "status": "retired", "title": "Reset",
         "first_seen_run_id": "r1", "last_revised_run_id": "r2"},
        {"id": "US-0002.1", "kind": "acceptance_criterion", "parent_us_id": "US-0002",
         "status": "retired", "description": "Resets", "first_seen_run_id": "r1",
         "last_revised_run_id": "r2", "coded_run_id": "r1", "coded_at": "t1"},
        {"id": "US-0003", "kind": "user_story", "status": "active",
         "title": "[Flaky test] something", "first_seen_run_id": "r2", "last_revised_run_id": "r2"},
        {"id": "US-0004.1", "kind": "acceptance_criterion", "parent_us_id": "US-0004",
         "status": "active", "description": "Orphaned undelivered", "first_seen_run_id": "r1",
         "last_revised_run_id": "r1"},
    ]
    rows = _us_ac_rows(ledger, {"US-0001"}, {"US-0001.1", "US-0001.2"}, "r2")
    by_id = {r["id"]: r for r in rows}
    assert by_id["US-0001.1"]["change"] == "unchanged" and by_id["US-0001.1"]["coded_run_id"] == "r1"
    assert by_id["US-0001.2"]["change"] == "modified" and by_id["US-0001.2"]["tested_run_id"] == "r2"
    assert by_id["US-0002"]["change"] == "deleted" and by_id["US-0002.1"]["change"] == "deleted"
    assert "US-0003" not in by_id, "flake-ticket synthetic stories (no AC children) must be filtered"
    assert "US-0004.1" not in by_id, "unchanged foreign AC outside own spec is not a row"
    assert by_id["US-0001"]["coded_run_id"] == "r2", "US coded = latest child stamp when all live children coded"
    assert _undelivered_ac_ids(ledger) == ["US-0004.1"], _undelivered_ac_ids(ledger)
    section = "\n".join(_render_us_ac_section(rows, _undelivered_ac_ids(ledger), "r2"))
    assert "## User stories & acceptance criteria this run" in section
    assert "| US-0001.2 | modified |" in section and "r2 (this run)" in section
    assert "Carried over -- not delivered**: US-0004.1" in section
    assert "(none recorded" in "\n".join(_render_us_ac_section([], [], "r2"))

    print("exit_nodes self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.exit_nodes`
    _demo()
