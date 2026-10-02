"""exit -- exit. One LLM judgment node (a StageSpec, ADVERSARIAL/audit pattern reused for
consistency with every other stage, even though the plan's own diagram sketched a single LLM box
-- an adversarial second opinion on "is this merge-ready" is worth having, same reasoning as every
other stage's audit pass) + one deterministic finalization node, exactly as the plan specifies for
the finalize half.

Verification status: NOT exercised against a real sandbox, same caveat as every quality-remediation+ node cluster
this session.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import shlex
from datetime import datetime, timezone
from typing import Any

from . import approvals, chat_model, git_ops, metrics_nodes, preflight_nodes, repo_files, repo_scan, session_store, spec_ledger, workflow_persistence
from . import config as workflow_config
from .gates import exit_readiness_checks
from .gates.checks import Check, CheckLog
from .markdown_render import render_exit_markdown
from .preflight_nodes import MANIFEST_PATH
from .sandbox.provider import SandboxProvider
from .text_truncate import truncate_middle
# Imported under this module's existing private name, not renamed at every call site: a LOCAL
# `from .tech_stack_signals import ... presence_values` inside verify_exit_readiness (a completely
# different, 2-arg function) shadows a bare module-level `presence_values` for that whole function
# body, so keeping the `_`-prefixed alias here is what avoids a same-named-but-wrong-arity
# collision, not just habit.
from .schemas import presence_values as _presence_values

logger = logging.getLogger(__name__)

# _GATE_OWNED_REASON_MARKERS, _MANIFEST_COMPLETENESS_TOPIC_KEYWORDS, _combined_test_command_from_apps
# and _manifest_completeness_topic all moved to gates/exit_readiness_checks.py (2026-09-30, Task 14)
# so the sandbox's own same-turn Stop hook (check-exit-readiness-stop.mjs) can shell out to the REAL
# implementations instead of a second, independently-drifting JS port -- see that module's own
# docstring. verify_exit_readiness below imports them back unchanged; this is a pure code-move.

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


def _parse_hook_fail_opens(raw: str | None) -> list[dict[str, Any]]:
    """Parses `repo_files.HOOK_FAIL_OPENS_PATH`'s content (a JSONL file -- one `{ts, hook, stage, reason}`
    object per line, written by every Stop hook's `reportFailOpen`) into a list of dicts.

    `raw` is `None` on the common case (file absent -- most sessions have zero fail-opens; see
    repo_files.read_repo_file, which already returns None for both "doesn't exist" and "can't be
    read"). A line that isn't valid JSON is skipped, not fatal -- one bad line (a hook's own write
    raced a truncation, say) must not blank out every OTHER hook's real fail-open in the same
    session's report."""
    if not raw:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _render_hook_fail_opens_section(fail_opens: list[dict[str, Any]] | None) -> list[str]:
    """Plain-language summary of Stop hooks that failed open this session (missing tool, timeout,
    or unparsable output -- see report-fail-open.mjs's own docstring). Empty list -- the common
    case, most sessions have zero -- renders NOTHING, not an empty heading; this is the one
    exception to the "unconditional heading" convention _render_eval_section/_render_skills_section
    use, deliberately, per this feature's own spec: a silent file must stay silent in the report."""
    if not fail_opens:
        return []
    lines = [
        "## Deterministic checks skipped this session",
        "",
        f"{len(fail_opens)} deterministic check(s) could not run this session and were skipped "
        "(a same-turn Stop hook failed open -- see `agent/sandbox-image/hooks/lib/"
        "report-fail-open.mjs`; the authoritative gate for each still ran normally):",
        "",
    ]
    for fo in fail_opens:
        hook = fo.get("hook", "?")
        stage = fo.get("stage", "?")
        reason = fo.get("reason", "?")
        ts = fo.get("ts", "?")
        lines.append(f"- `{hook}` ({stage}, {reason}) at {ts}")
    lines.append("")
    return lines


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
            shown = ", ".join(f"`{i}`" for i in items[:workflow_config.EXIT_SBOM_DIFF_PREVIEW_MAX])
            more = (
                f" ... and {len(items) - workflow_config.EXIT_SBOM_DIFF_PREVIEW_MAX} more"
                if len(items) > workflow_config.EXIT_SBOM_DIFF_PREVIEW_MAX else ""
            )
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
    return first[:workflow_config.EXIT_FAILURE_HEADLINE_CHARS]


def _terminal_failure_reason(run_failure: dict[str, Any]) -> str:
    """The blocking bullet exit_finalize_node injects for a terminal failure -- starts with
    exit_readiness_checks.TERMINAL_FAILURE_MARKER, so a later attempt's stale filter owns it."""
    reason = f"{exit_readiness_checks.TERMINAL_FAILURE_MARKER} {run_failure.get('stage')}: {run_failure.get('type')}"
    detail_line = _failure_headline(run_failure)
    return f"{reason} -- {detail_line}" if detail_line else reason


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
        truncated = truncate_middle(
            detail, workflow_config.EXIT_FAILURE_DETAIL_HEAD_CHARS, workflow_config.EXIT_FAILURE_DETAIL_TAIL_CHARS
        )
        lines += ["", "```", truncated, "```"]
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


def _reconcile_divergence_risk_notes(divergence_rows: list[dict[str, Any]], existing_risk_notes: list[str]) -> list[str]:
    """Deterministic reconciliation for exit_finalize_node: the drafting model writes its
    merge_readiness prose WITHOUT ever seeing divergence_rows (computed later, from the ledger,
    by a separate code path) -- observed live, it claimed divergence findings were "fixed and
    re-verified" while this exact computation still showed them open, and nothing caught the
    contradiction because nothing compared the two. Pure so the reconciliation itself is
    self-checkable without a sandbox.

    Every open row lands in risk_notes, never blocking_reasons: by construction it is always
    severity minor (adversarial_gate blocks the stage from ever approving while a critical/major
    finding remains open -- see BLOCKING_SEVERITIES), the same non-gating bucket the exit prompt
    already uses for a scanner finding marked gating: false. This must never flip merge_ready.
    """
    notes = [
        f"adversarial-compliance divergence still OPEN: {row['plan_reference']}: {row['description']} "
        f"-- {row['proposed_resolution'] or '(no proposed resolution given)'}"
        for row in divergence_rows
        if row.get("status") == "open"
    ]
    return existing_risk_notes + [n for n in notes if n not in existing_risk_notes]


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
            notes.append(
                f"tool run failed: "
                f"{str(row.get('error') or row.get('summary') or '')[:workflow_config.EXIT_TOOL_ERROR_SNIPPET_CHARS]}"
            )
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
            # Real stage status when this row's "stage" is one (specification, remediation, ...);
            # "-" for a ledger-only tag with no real-stage counterpart (red-gate, rebuild, unknown).
            "status": (stages or {}).get(entry["stage"], {}).get("status") or "-",
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
        "| Stage | Status | Runtime | Laps | Tokens in/out | Cost | Notes |",
        "|---|---|---|---|---|---|---|",
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
        lines.append(f"| {r['stage']} | {r['status']} | {_fmt_duration(r['runtime_seconds'])} | {r['laps'] or '-'} | {tokens} | {cost} | {notes} |")
    lines.append(f"| **Total** | | **{_fmt_duration(total_seconds)}** | | | **{'$' + format(total_cost, '.2f') if any_cost else 'n/a'}** | |")
    return report_rows, "\n".join(lines) + "\n"


def _md_cell(text: Any, limit: int = workflow_config.EXIT_MD_CELL_DEFAULT_CHARS) -> str:
    """Same |-escape + truncation idiom the US/AC table uses."""
    cell = str(text if text is not None else "").replace("\n", " ").replace("|", "\\|")
    return cell[: limit - 3] + "..." if len(cell) > limit else cell


def _fmt_tool_duration(ms: Any) -> str:
    return f"{int(ms) / 1000:.1f}s" if isinstance(ms, (int, float)) else "--"


def _finding_disposition(finding: dict[str, Any], known_gaps: list[str]) -> str:
    """One honest label per remaining finding -- the user-facing answer to 'why is this still
    here'. Order matters: an explained finding is a known gap even if it would also be exempt."""
    from .gates.remediation_gate import accounted_for  # lazy: keeps gate imports one-directional

    finding_id = str(finding.get("id"))
    if accounted_for(finding_id, known_gaps):
        gap = next((str(g) for g in known_gaps if finding_id.lower() in str(g).lower()), "")
        reason = gap.split(finding_id, 1)[-1].lstrip(" :--") if finding_id in gap else gap
        return _md_cell(f"known gap: {reason}" if reason else "known gap", workflow_config.EXIT_KNOWN_GAP_CELL_CHARS)
    if finding.get("actionable"):
        return "open -- introduced after remediation, no disposition recorded"
    path = (finding.get("location") or {}).get("path")
    if repo_scan.is_non_application_path(path):
        return "outside application code"
    if repo_scan.is_advisory_rule(finding.get("rule_id")):
        return "advisory rule"
    if finding.get("category") == "license" and repo_scan.is_transitive_dependency_file(path):
        return "transitive lockfile licence"
    return "pre-existing quality debt (in baseline)"


_FINDINGS_TABLE_CAP = workflow_config.EXIT_FINDINGS_TABLE_CAP


def _render_scan_sections(scan_report: dict[str, Any] | None, remediation: dict[str, Any] | None) -> list[str]:
    """`## Health score` + `## Findings (N clusters)` + `## Scanner tools (N)`, from the dashboard
    dict metrics_compute persisted into metrics-latest.json. Deterministic, fixed skeleton: on a
    run that never reached metrics_compute the sections say so instead of silently missing."""
    if not scan_report:
        return [
            "## Health score", "",
            "_scan sections unavailable -- metrics_compute did not run this attempt._", "",
        ]
    summary = scan_report.get("summary") or {}
    findings = scan_report.get("findings") or []
    tools = scan_report.get("tools") or []
    # known_gaps is PresenceList-shaped as of Task 11 ({"status", "values", "reason"}), not a bare
    # list[str] -- _presence_values (below) unwraps it, same tolerant read as blocking_reasons/
    # risk_notes elsewhere in this module.
    known_gaps = [str(g) for g in _presence_values((remediation or {}).get("known_gaps"))]

    lines: list[str] = ["## Health score", ""]
    score = summary.get("health_score")
    fraction = summary.get("health_coverage_fraction")
    multiplier = summary.get("health_coverage_multiplier")
    criticals = summary.get("active_critical_count")
    headline = f"**{score if score is not None else '--'} / 100**"
    details = [
        f"raw {summary.get('health_raw', '--')}",
        f"coverage multiplier {multiplier if multiplier is not None else '--'}"
        + (f" ({fraction:.0%} of security tools completed)" if isinstance(fraction, (int, float)) else ""),
        # The research note's "critical override" -- always 'no' by design; the count is banner-only.
        f"critical override: no ({criticals if criticals is not None else '--'} active critical(s))",
    ]
    if isinstance(summary.get("kloc"), (int, float)):
        details.append(f"{summary['kloc']} kloc")
    lines += [headline + " -- " + " · ".join(details), ""]
    subscores = summary.get("health_subscores") or {}
    weights_used = summary.get("health_weights_used") or {}
    basis = summary.get("health_basis") or {}
    if subscores:
        lines += ["| Dimension | Weight used | Score | Basis |", "|---|---|---|---|"]
        for name in repo_scan.HEALTH_DIMENSION_NAMES:
            if name not in subscores:
                continue
            sub = subscores.get(name)
            weight = weights_used.get(name)
            lines.append(
                f"| {name} | {f'{weight:.0%}' if isinstance(weight, (int, float)) else '--'} "
                f"| {sub if sub is not None else '--'} "
                f"| {_md_cell(basis.get(name) or ('unmeasured, weight redistributed' if sub is None else ''), workflow_config.EXIT_HEALTH_BASIS_CELL_CHARS)} |"
            )
        lines.append("")

    lines += [f"## Findings ({len(findings)} clusters)", ""]
    if findings:
        addressed = len(_presence_values((remediation or {}).get("findings_addressed")))
        explained = sum(1 for f in findings if _finding_disposition(f, known_gaps).startswith("known gap"))
        open_count = sum(
            1 for f in findings
            if f.get("actionable") and _finding_disposition(f, known_gaps).startswith("open")
        )
        exempt = len(findings) - explained - open_count
        lines += [
            f"{addressed} fixed by remediation this run · {explained} known gap(s) · "
            f"{exempt} auto-exempt · {open_count} open",
            "",
            "| Severity | Category | Tool(s) | Rule | Title | Location | Gating | Disposition |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for finding in findings[:_FINDINGS_TABLE_CAP]:
            location = finding.get("location") or {}
            where = location.get("path") or "?"
            if location.get("start_line"):
                where += f":{location['start_line']}"
            lines.append(
                f"| {finding.get('severity')} | {finding.get('category')} "
                f"| {_md_cell(', '.join(finding.get('tools') or []), workflow_config.EXIT_FINDING_TOOLS_CELL_CHARS)} "
                f"| {_md_cell(finding.get('rule_id') or finding.get('cve') or '', workflow_config.EXIT_FINDING_RULE_ID_CELL_CHARS)} "
                f"| {_md_cell(finding.get('title'), workflow_config.EXIT_FINDING_TITLE_CELL_CHARS)} "
                f"| {_md_cell(where, workflow_config.EXIT_FINDING_WHERE_CELL_CHARS)} "
                f"| {'yes' if finding.get('gating') else 'no'} "
                f"| {_finding_disposition(finding, known_gaps)} |"
            )
        if len(findings) > _FINDINGS_TABLE_CAP:
            lines.append(f"| ... | | | | and {len(findings) - _FINDINGS_TABLE_CAP} more | see repo-scan-latest.json | | |")
    else:
        lines.append("No findings in the final scan.")
    lines.append("")

    lines += [f"## Scanner tools ({len(tools)})", ""]
    if tools:
        lines += ["| Tool | Version | State | Duration | Findings | Notes |", "|---|---|---|---|---|---|"]
        for tool in tools:
            lines.append(
                f"| {tool.get('name')} | {_md_cell(tool.get('version') or '--', workflow_config.EXIT_TOOL_VERSION_CELL_CHARS)} "
                f"| {str(tool.get('status') or '--').upper()} | {_fmt_tool_duration(tool.get('duration_ms'))} "
                f"| {tool.get('findings', '--')} | {_md_cell(tool.get('notes') or '', workflow_config.EXIT_TOOL_NOTES_CELL_CHARS)} |"
            )
    else:
        lines.append("No tool run records in this scan.")
    lines.append("")
    return lines


def _render_score_explanations(metrics_summary: dict[str, Any] | None) -> list[str]:
    """Every number on the split Metrics Bar (Code Health / App Health / AI Dev Workflow Framework
    Effectiveness) gets a matching 'how we got this' block here -- no bar number without a paper
    trail. Pure rendering: every value printed already exists in metrics_nodes.py's computations
    (metrics_compute_node), nothing is recomputed here."""
    metrics_summary = metrics_summary or {}
    lines = ["### How these scores were calculated", ""]

    code_health = metrics_summary.get("code_health_score")
    subscores = metrics_summary.get("code_health_subscores") or {}
    subscore_str = ", ".join(f"{k}={v}" for k, v in sorted(subscores.items()) if v is not None)
    lines.append(
        f"- **Code Health ({code_health if code_health is not None else '--'})**: static analysis only "
        "(security, dependencies, complexity, duplication, maintainability -- the same tool set the "
        "standalone Code Health Report runs, no coverage or live-app measurement). Subscores: "
        + (subscore_str or "not measured this run.")
    )

    app_health = metrics_summary.get("app_health_score")
    app_inputs = metrics_summary.get("app_health_inputs") or {}
    # B4 (tech-stack startability pivot): a repo the B2 boot probe found non-startable gets this
    # branch instead of the normal coverage/pass-rate blend wording below -- parallel to that
    # branch's own "unmeasured" phrasing, not a second None-handling mechanism.
    not_startable_reason = app_inputs.get("not_startable_reason")
    if not_startable_reason:
        lines.append(f"- **App Health (--)**: unavailable -- app not startable: {not_startable_reason}")
    else:
        cov_frac, pass_frac = app_inputs.get("coverage_fraction"), app_inputs.get("pass_rate_fraction")
        lines.append(
            f"- **App Health ({app_health if app_health is not None else '--'})**: blend of test coverage "
            f"({f'{cov_frac * 100:.1f}%' if cov_frac is not None else 'unmeasured'}) and whole-suite test pass rate "
            f"({f'{pass_frac * 100:.1f}%' if pass_frac is not None else 'unmeasured'}), weighted "
            f"{workflow_config.AIDW_APP_HEALTH_COVERAGE_WEIGHT}/{workflow_config.AIDW_APP_HEALTH_PASS_RATE_WEIGHT}. "
            "DAST scanning is not yet part of this score."
        )

    ac_resolution = metrics_summary.get("ac_resolution") or {}
    resolution_line = (
        f"- **AI Dev Workflow Framework Effectiveness -- AC Resolution ({ac_resolution.get('score', '--')}%)**: "
        f"{ac_resolution.get('resolved', 0)} of {ac_resolution.get('total', 0)} ACs resolved "
        f"({ac_resolution.get('deferred_excluded', 0)} deferred-and-never-coded, excluded from the denominator)."
    )
    if ac_resolution.get("deferred_after_coding"):
        resolution_line += (
            f" **{ac_resolution['deferred_after_coding']} AC(s) were deferred AFTER coding started -- still "
            "counted as unresolved** (a late deferral cannot remove an AC from the score for free)."
        )
    lines.append(resolution_line)

    productivity = metrics_summary.get("productivity_estimate") or {}
    if productivity:
        lines.append(
            "- **AI Dev Workflow Framework Effectiveness -- Productivity / Effort-Saved Estimate "
            f"(~{productivity.get('estimated_hours_saved', '--')} hours)**: Capability-Based Lifecycle "
            f"Benchmarking -- sizing: {productivity.get('added_lines', 0)} lines changed this ticket; "
            f"effort translation: mean cyclomatic complexity {productivity.get('mean_ccn', '--')} applies a "
            f"{productivity.get('complexity_multiplier', 1.0)}x hours-per-line multiplier; coverage discount: "
            f"the AC Resolution score above ({productivity.get('ac_resolution_discount', '--')}%) stands in for "
            "semantic AC-match scoring, since resolution here is already deterministic, not text-similarity-"
            f"based; less a flat {productivity.get('review_overhead_fraction', 0) * 100:.1f}% review-overhead "
            "deduction for human review of AI-generated code."
        )
    lines.append("")
    return lines


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
    remediation: dict[str, Any] | None = None,
    traceability_matrix_markdown: str | None = None,
    hook_fail_opens: list[dict[str, Any]] | None = None,
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
    lines += _render_score_explanations(metrics_summary)
    e2e = e2e or {}
    e2e_status = e2e.get("status") or "not run"
    e2e_line = f"- **E2E**: {e2e_status}"
    if e2e.get("failed_tests"):
        e2e_line += f" ({len(e2e['failed_tests'])}/{e2e.get('total', 0)} failed)"
    elif e2e.get("skipped_reason"):
        e2e_line += f" -- {e2e['skipped_reason']}"
    lines.append(e2e_line)
    lines.append("")

    # The scan sections: total score + per-dimension table, every finding cluster with its
    # disposition, and the per-tool run table. All from the dashboard dict metrics_compute
    # persisted; on an escalated run that never reached metrics_compute they say so.
    lines += _render_scan_sections((metrics_summary or {}).get("repo_scan"), remediation)

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
    if traceability_matrix_markdown:
        lines += ["", traceability_matrix_markdown.rstrip("\n"), ""]
    lines += _render_supply_chain_section(metrics_summary)
    lines += _render_skills_section(stages)
    lines += _render_hook_fail_opens_section(hook_fail_opens)

    lines += ["## Screens", ""]
    # Wireframe-coverage figure, stamped by e2e_nodes.check_wireframe_coverage: `total` is None
    # when there was nothing to check (no wireframes in the approved plan, or it was unreadable),
    # distinct from an empty `missing` list, which means every wireframed screen was proven. This
    # is the number a reviewer actually needs -- it can no longer silently diverge from the plan
    # the way a raw screenshot-file count once did (14 files vs. 6 wireframes, one real incident).
    wireframe_total = e2e.get("wireframe_coverage_total")
    if wireframe_total is not None:
        missing = e2e.get("wireframe_coverage_missing") or []
        verified = wireframe_total - len(missing)
        lines.append(f"**Wireframe coverage**: {verified}/{wireframe_total} wireframed screens verified in e2e.")
        if missing:
            lines.append(
                "Missing: " + ", ".join(f"{wf['screen']} ({', '.join(wf['ac_ids']) or 'no ac_ids'})" for wf in missing) + "."
            )
        lines.append("")
    unwireframed = e2e.get("unwireframed_screens") or []
    if unwireframed:
        lines.append(
            f"**Additional e2e evidence with no matching wireframe** (advisory, not blocking): "
            f"{', '.join(unwireframed)}."
        )
        lines.append("")
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
                    # None (renders "-") for a ledger entry that predates this field, never a
                    # fabricated guess -- see spec_ledger.py's sync/construction sites.
                    "ui_related": e.get("ui_related"),
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
        lines += ["| Id | Change | Title / Description | UI | Coded (run) | Tested (run) | Tests |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            desc = _md_cell(r.get("title_or_description") or "", 90)
            if r.get("kind") == "user_story":
                desc = f"**{desc}**"
            ui_related = r.get("ui_related")
            ui = "Yes" if ui_related is True else "No" if ui_related is False else "--"
            coded = r.get("coded_run_id") or "--"
            if coded != "--" and r.get("coded_run_id") == run_id:
                coded = f"{coded} (this run)"
            tested = r.get("tested_run_id") or "--"
            if tested != "--" and r.get("tested_run_id") == run_id:
                tested = f"{tested} (this run)"
            tests = ", ".join((r.get("test_ids") or [])[:workflow_config.EXIT_TEST_IDS_PREVIEW_MAX])
            extra = len(r.get("test_ids") or []) - workflow_config.EXIT_TEST_IDS_PREVIEW_MAX
            if extra > 0:
                tests += f", +{extra} more"
            lines.append(
                f"| {r['id']} | {r.get('change')} | {desc} | {ui} | {coded} | {tested} | {tests or '--'} |"
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


def _retired_scope_file_review(ledger_entries: list[dict[str, Any]], retired_ids: list[str]) -> list[str]:
    """Requirements-delta pivot (A4): application/production code cleanup for a retired US/AC is
    best-effort/prompted (minimal_code_to_green_brownfield_segment.md), not deterministically
    gated the way retired-AC test residue already is (ac_coverage_gate.check_retired_ac_residue).
    This is the safety net for whatever the model didn't catch: one review line per retired id,
    naming the files its ledger entry recorded (`spec_ledger.append_files`) so a human reviewer has
    a concrete starting point rather than the pipeline guessing what's safe to delete.

    A retired User Story's own ledger entry never carries a `files` field (only its child
    Acceptance Criterion entries do -- `append_files` only ever matches `kind ==
    "acceptance_criterion"`), and the `retired_us_ids` cascade only flips each child AC's status
    without copying anything onto the parent -- so a retired story's file list is assembled here by
    filtering ledger entries on `parent_us_id`, not by reading a `files` field off the story entry
    itself (there isn't one)."""
    by_id = {e["id"]: e for e in ledger_entries}
    lines: list[str] = []
    for rid in sorted(retired_ids):
        entry = by_id.get(rid)
        if entry is None:
            continue
        if entry.get("kind") == "acceptance_criterion":
            files = entry.get("files") or []
            label = entry.get("description", "")
        else:
            files = [
                f for child in ledger_entries if child.get("parent_us_id") == rid for f in (child.get("files") or [])
            ]
            label = entry.get("title", "")
        if files:
            file_list = ", ".join(sorted({f.get("path", "") for f in files if isinstance(f, dict) and f.get("path")}))
            if file_list:
                lines.append(f"- {rid} ({label}): {file_list}")
    return lines


def _presence_from_values(values: list[str], *, empty_reason: str) -> dict[str, Any]:
    """Build a PresenceList-shaped dict from a plain list -- the read-modify-rewrite counterpart to
    schemas.presence_values, for the two fields this stage mutates in place after the model's initial
    (already-typed) output: `status='present'` for a non-empty list, `status='absent'` with
    `empty_reason` otherwise. Never leaves `values` populated while `status='absent'`, and never
    reports `status='present'` with nothing in `values` -- the shape PresenceList.model_validator
    itself would reject."""
    if values:
        return {"status": "present", "values": values, "reason": ""}
    return {"status": "absent", "values": [], "reason": empty_reason}


# _targeted_fix_unresolved_problems moved to gates/exit_readiness_checks.py (2026-09-30, Task 14) --
# verify_exit_readiness below imports it back unchanged.

# Task 13b: one line per DISTINCT condition inside verify_exit_readiness below that forces
# merge_ready=False (this gate always returns passed=True to the graph -- an LLM redraft cannot
# fix a code regression or a missing screenshot, so the downgrade IS the outcome -- but each
# condition below is still a real, distinct reason the merge gets blocked, and the model's own
# blocking_reasons/merge_ready should agree with it). Nine: four independent manifest/evidence
# presence checks, the metrics-not-recorded-for-this-run guard, the regression gate's own
# recorded reasons (its granular thresholds live in a different module and are not enumerated
# here), the README-ownership-scoped check, the auth-verification requirement, and (added
# 2026-09-12, "rescue mechanism" work) a prior targeted-fix attempt's own still-open reasons.
METRICS_EXIT_HARD_RULES: tuple[str, ...] = (
    "manifest.json must record at least one runnable app via app_check.apps (unless "
    "app_check explicitly marked the repo unsuitable) -- an empty app list after the re-scan "
    "blocks the merge.",
    "manifest.json must record a test_command for this stack -- a missing one blocks the "
    "merge.",
    "manifest.json must record coverage_commands (the replayable coverage contract) -- "
    "without it coverage cannot be replayed and the merge is blocked.",
    "A UI application must have captured at least one e2e screenshot -- zero screenshots "
    "blocks the merge.",
    "Code metrics must actually have been recorded for THIS run (a matching run_id) -- "
    "unrecorded metrics block the merge on their own.",
    "Whatever reasons the metrics regression gate recorded for this run (coverage, "
    "duplication, security thresholds, etc.) must be empty -- any recorded reason blocks the "
    "merge.",
    "When this leg owns the README (not a human-authored brownfield README), any README "
    "problem still open after its own retry laps blocks the merge.",
    "If authentication enforcement was required for this run, the e2e auth probe must "
    "actually have run and passed -- required auth that was never verified blocks the merge.",
    "If a prior targeted-fix attempt against THIS run's own reasons left any of them "
    "independently confirmed still open, those reasons block the merge again -- a redraft's own "
    "say-so is not enough to clear them.",
)


# Per-sub-check rows for verify_exit_readiness. All advisory: this verify always passes and a
# problem downgrades merge_ready instead of bouncing the draft, so a problem records "advisory".
EXIT_MANIFEST = Check(
    "exit.manifest", "Manifest records app, tests and coverage",
    "manifest.json must name at least one runnable app, a test command and replayable coverage "
    "commands. Without them the next run or reviewer can't rebuild and re-test what was merged.",
    "advisory",
)
EXIT_SCREENSHOTS = Check(
    "exit.screenshots", "UI screenshots captured",
    "An app with a user interface must have at least one end-to-end screenshot. Passing tests alone "
    "don't show that a page actually renders, so a person needs the pictures.",
    "advisory", condition="only for apps with a user interface",
)
EXIT_METRICS = Check(
    "exit.metrics", "No metrics regression",
    "Code metrics must have been recorded for this run and the regression gate (coverage, duplication, "
    "security, README) must report nothing. Any regression it names blocks the merge.",
    "advisory",
)
EXIT_TARGETED_FIX = Check(
    "exit.targeted_fix", "Targeted fix resolved its issues",
    "If a targeted fix ran against this run's problems, every problem it was sent to fix must be "
    "independently confirmed closed. The fix's own claim of success isn't enough.",
    "advisory", condition="only after a targeted fix ran this run",
)
EXIT_AUTH = Check(
    "exit.auth", "Required authentication verified",
    "When the app is required to enforce sign-in, the end-to-end auth probe must have run and passed. "
    "Auth that was required but never tested counts as unverified.",
    "advisory", condition="only when the app must enforce sign-in",
)
VERIFY_CHECKS: tuple[Check, ...] = (EXIT_MANIFEST, EXIT_SCREENSHOTS, EXIT_METRICS, EXIT_TARGETED_FIX, EXIT_AUTH)


def _record_advisory(log: CheckLog, check: Check, problems: list[str], detail: str | None = None) -> None:
    if problems:
        log.advisory(check, "\n".join(problems))
    else:
        log.passed(check, detail)


async def verify_exit_readiness(
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: Any,
    _chat_provider: str, _lap: int = 0, _audit_ran_this_lap: bool = True, *, log: CheckLog | None = None,
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
    from .graph import TARGETED_FIX_UNRESOLVED_PATH, VerificationResult  # local: graph imports exit_nodes (same pattern as audit_gates)
    from .tech_stack_signals import frameworks_have_ui, presence_values

    log = log or CheckLog("metrics-exit_verify", VERIFY_CHECKS)

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
    # Decision logic (what belongs in `updates`) lives in exit_readiness_checks.resolve_manifest_updates
    # (2026-09-30, Task 14); the conditional guards below stay HERE so the expensive
    # app_discovery.collect_evidence sandbox scan is still only called when apps are genuinely
    # missing, same cost profile as before this extraction.
    resolved_apps = None
    scan_fingerprint = None
    app_check = manifest.get("app_check") or {}
    if not (app_check.get("apps") or []):
        # app_check_record ran pre-scaffold (empty [] on greenfield, by construction) -- re-scan
        # now that the code exists, exact reuse of e2e_gate_check_node's greenfield re-scan.
        scan = await app_discovery.collect_evidence(provider, thread_id)
        resolved_apps = app_discovery.candidates_to_apps(scan.get("candidates") or [])
        scan_fingerprint = scan.get("fingerprint")
    resolved_test_command = None
    if not manifest.get("test_command"):
        resolved_test_command = resolve_test_command(tech_stack) or exit_readiness_checks.combined_test_command_from_apps(
            app_check.get("apps") or []
        )
    coverage_entries = None
    if not manifest.get("coverage_commands"):
        coverage_entries = _parse(await repo_files.read_repo_file(provider, thread_id, workflow_config.COVERAGE_COMMANDS_PATH)).get("entries")
    updates = exit_readiness_checks.resolve_manifest_updates(
        manifest, resolved_apps=resolved_apps, scan_fingerprint=scan_fingerprint,
        resolved_test_command=resolved_test_command, coverage_entries=coverage_entries,
    )
    if updates:
        manifest = await preflight_nodes.update_manifest(provider, thread_id, updates)

    # --- presence: what a merge actually needs recorded ---
    problems = exit_readiness_checks.manifest_presence_problems(manifest)
    _record_advisory(log, EXIT_MANIFEST, problems)

    # --- screenshots: mandatory visual evidence for UI apps, whatever path e2e took (covers all
    # of its skip paths with one check) ---
    is_ui = frameworks_have_ui(presence_values(tech_stack, "frameworks"))
    screenshots = await _list_screenshots(provider, thread_id, run_id)
    screenshot_probs = exit_readiness_checks.screenshot_problems(is_ui, len(screenshots))
    problems += screenshot_probs
    if is_ui:
        _record_advisory(log, EXIT_SCREENSHOTS, screenshot_probs, f"{len(screenshots)} screenshot(s)")
    else:
        log.skipped(EXIT_SCREENSHOTS, "no UI framework in the tech stack")

    # --- the metrics regression gate's verdict, run-id-stamped so a stale file never gates ---
    metrics = _parse(await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/metrics-latest.json"))
    metrics_probs, metrics_matched = exit_readiness_checks.metrics_problems(metrics, run_id)
    problems += metrics_probs
    _record_advisory(log, EXIT_METRICS, metrics_probs)

    # --- independent post-targeted-fix verification (closes _run_targeted_fix's own previously
    # documented gap: "no independent, deterministic regression gate runs after this") ---
    targeted_fix_unresolved = _parse(await repo_files.read_repo_file(provider, thread_id, TARGETED_FIX_UNRESOLVED_PATH))
    targeted_probs = exit_readiness_checks.targeted_fix_unresolved_problems(targeted_fix_unresolved, run_id)
    problems += targeted_probs
    if targeted_fix_unresolved.get("run_id") == run_id:
        _record_advisory(log, EXIT_TARGETED_FIX, targeted_probs)
    else:
        log.skipped(EXIT_TARGETED_FIX, "no targeted-fix attempt recorded for this run")

    # --- auth enforcement can't silently vanish (W4): a run that REQUIRED auth but whose e2e
    # never ran (non-UI repo, runner missing, suite skipped) verified nothing -- exactly the
    # repos (API-only) where auth matters most. A named blocker, not a silent pass. Read from
    # metrics-latest.json (metrics_compute persists app_auth + the e2e snapshot for exactly this
    # check) -- deterministic verifies are file-based, never graph-state-based. Gated behind the
    # same run_id match as the regression-gate check above (metrics_matched).
    if not metrics_matched:
        log.skipped(EXIT_AUTH, "metrics were not recorded for this run, so auth could not be checked")
    if metrics_matched:
        auth_probs, auth_note = exit_readiness_checks.auth_problems(metrics, workflow_config.AIDW_AUTH_GATE)
        problems += auth_probs
        if auth_probs or auth_note:
            _record_advisory(log, EXIT_AUTH, auth_probs, auth_note)
        else:
            log.skipped(EXIT_AUTH, "authentication enforcement not required for this run")
        if auth_note:
            # Verified: surface the gate's per-route verdict summary in the exit report (via the
            # report's own risk-notes section) -- the "reported, not blocking" inconclusives
            # otherwise live only in metrics-latest.json.
            notes = _presence_values(content_dict.get("risk_notes"))
            if auth_note not in notes:
                content_dict["risk_notes"] = _presence_from_values(
                    notes + [auth_note], empty_reason="no risk notes recorded"
                )

    # Drop STALE deterministic blockers the model carried over from a previous run's report, and
    # decide the final merge_ready verdict -- exit_readiness_checks.evaluate_merge_readiness (Task
    # 14) is the same pure logic that used to live inline here; see that function's own docstring
    # for the full "why" (income-investor runs 45e08f64/c1458b23/f0fef8ba, three separate real
    # staleness bugs this filter exists to prevent).
    verdict = exit_readiness_checks.evaluate_merge_readiness(
        problems, content_dict.get("blocking_reasons"), content_dict.get("merge_ready")
    )
    stale_reasons = verdict["stale_reasons"]
    if stale_reasons:
        logger.warning(
            "exit verify: dropping %d blocking reason(s) this run's regression gate did not raise "
            "(carried over from an earlier report): %s",
            len(stale_reasons),
            "; ".join(r[:workflow_config.EXIT_STALE_REASON_LOG_CHARS] for r in stale_reasons),
        )
    if verdict["blocking_reasons"] is not None:
        content_dict["blocking_reasons"] = verdict["blocking_reasons"]
    if verdict["merge_ready"] is not None:
        content_dict["merge_ready"] = verdict["merge_ready"]
        if verdict["merge_ready"] is True:
            # Every deterministic check passed AND every blocker the model listed was a stale copy
            # of a gate reason this run did not produce -- the False verdict was inherited rather
            # than earned. Left alone, this is precisely the "Ready to merge: False on a clean
            # tree" outcome that sends a human hunting for a defect that was already fixed.
            logger.warning(
                "exit verify: merge_ready flipped False -> True -- every deterministic check passed "
                "and all %d model-supplied blocker(s) were stale gate reasons from an earlier run",
                len(stale_reasons),
            )
    feedback = verdict["feedback"]
    return VerificationResult(
        passed=True,
        feedback=feedback,
        report={"blockers": problems, "ui_app": is_ui, "screenshot_count": len(screenshots), "manifest_completed": sorted(updates)},
        checks=[r.to_dict() for r in log.results()],
    )


def metrics_exit_judged_this_attempt(state: dict[str, Any]) -> bool:
    """Whether metrics-exit's own draft+verify ran in THIS attempt (since the last intake), i.e.
    whether its approved_content was written about this attempt at all.

    run_id can't answer this: a resume keeps it (intake_node's return), so yesterday's failed
    attempt and today's resumed one share it. The signal is `last_verification`: intake_node's
    _reset_stage_mechanics clears it on EVERY intake (resume or not, after hydration), and only
    metrics-exit's verify node sets it again -- which runs after every real draft of this stage in
    every mode (its Gate is persists=True, which graph._demo asserts is never "off"). The resume
    short-circuit (make_draft_node's should_skip_draft branch) used to re-fire exit_finalize_node
    with the previous attempt's approved_content without ever reaching verify; metrics-exit's
    StageSpec.redraft_every_attempt now refuses that skip on exactly this False signal."""
    return ((state.get("stages") or {}).get("metrics-exit") or {}).get("last_verification") is not None


def _deterministic_only_report(run_id: str) -> dict[str, Any]:
    """The MergeReadinessReport seed exit_finalize_node uses when metrics-exit did not judge this
    attempt: none of the prior attempt's model-written prose (blocking reasons, risk notes, PR
    title/description -- session c2bbdca1's said "scaffold only", "no main.ts", "zero e2e
    screenshots" about a tree that had all of them by then). Pessimistic by construction:
    NOT_RECHECKED_REASON keeps merge_ready False unless verify_exit_readiness actually runs, and is
    gate-owned, so that run clears it and leaves only this attempt's real blockers."""
    return {
        "merge_ready": False,
        "blocking_reasons": _presence_from_values(
            [exit_readiness_checks.NOT_RECHECKED_REASON], empty_reason="unreachable: seeded non-empty"
        ),
        "pr_title": f"ai-dev-workflow: {run_id}",
        "pr_description_markdown": (
            "The merge-readiness review (metrics-exit) did not run in this attempt, so this report "
            "carries no model-written assessment: the verdict below comes only from this attempt's "
            "deterministic exit checks and any terminal pipeline failure."
        ),
        "risk_notes": _presence_from_values(
            [], empty_reason="no model-written risk review in this attempt (metrics-exit did not run)"
        ),
    }


async def _final_merge_readiness(
    thread_id: str, content: dict[str, Any], state: dict[str, Any], provider: Any
) -> dict[str, Any]:
    """The merge_readiness every exit_finalize_node consumer reads (manifest.json, report.json, the
    three exit markdowns, the PR title/body, close_session): `content` when metrics-exit judged
    this attempt, otherwise _deterministic_only_report -- then the deterministic recompute, then
    the terminal-failure injection. Mutates `content` in place on the judged path (as before)."""
    run_id = state.get("run_id", "unknown")
    if metrics_exit_judged_this_attempt(state):
        merge_readiness = content
    else:
        logger.warning(
            "exit finalize: metrics-exit did not run in this attempt (thread_id=%s, run_id=%s) -- "
            "discarding its prior-attempt report text, building merge readiness from deterministic checks only",
            thread_id, run_id,
        )
        merge_readiness = _deterministic_only_report(run_id)

    # Re-run the deterministic merge-readiness recompute UNCONDITIONALLY, on every single call --
    # not just a fresh approval. This is the fix for a real bug: on a RESUMED run whose metrics-exit
    # stage was already "approved" from an earlier attempt, make_draft_node's resume short-circuit
    # (graph.py's "Resume short-circuit" comment) re-fires this hook with the PREVIOUS run's frozen
    # approved_content, never calling verify_exit_readiness again. Root-caused live (income-investor
    # thread f0fef8ba): after the gitleaks/e2e failures that ORIGINALLY set blocking_reasons were
    # fixed on a later resume, the exit report kept re-reporting all of them forever. That fix only
    # dropped STALE gate-owned phrases; the model's own prose about the old attempt survived it
    # (session c2bbdca1) -- hence the deterministic-only seed above. Calling it again is a no-op on
    # the NORMAL (fresh-approval) path, where it already ran seconds earlier via make_verify_node --
    # an idempotent recompute, not a redraft. It DOES mean a second round of live sandbox reads
    # (manifest, tech-stack, coverage-commands, metrics-latest.json, a screenshot listing) on every
    # run that reaches this stage -- an honest, deliberate cost, not free.
    #
    # Guarded, not left to propagate: verify_exit_readiness does live, unguarded sandbox I/O, and
    # this codebase already documents provider.exec_in_sandbox raising RuntimeError against a
    # torn-down container. Propagating would skip exit_finalize_node's guaranteed close_session. On
    # failure, the content stays unrefreshed (judged path) or keeps NOT_RECHECKED_REASON (seed).
    try:
        await verify_exit_readiness(thread_id, merge_readiness, run_id, None, provider, "", 0)
    except Exception:  # noqa: BLE001 -- degrade, never abort the report
        logger.warning("exit finalize: verify_exit_readiness re-check failed for thread_id=%s", thread_id, exc_info=True)

    terminal_failure = state.get("run_failure")
    if terminal_failure:
        # A terminal escalate (rebuild/e2e/test-hardening) routed into this stage so the report
        # still gets written -- but the drafting model never sees run_failure, so without this the
        # report blames whatever incidental gaps it found ("metrics were not recorded") and never
        # names the actual killer. The bullet carries the error's first meaningful line -- a bare
        # "rebuild_cap_exceeded" sent the drafting model guessing at a root cause (observed live,
        # run d16959d3: it blamed a missing project reference; the real killer was an MSB4025
        # XML-comment error that only the DB row and ledger named). The full tail lands in its own
        # section of the report.
        reason = _terminal_failure_reason(terminal_failure)
        existing_reasons = _presence_values(merge_readiness.get("blocking_reasons"))
        if reason not in existing_reasons:
            merge_readiness["blocking_reasons"] = _presence_from_values(
                [reason, *existing_reasons],
                empty_reason="unreachable: reason is always appended in this branch",
            )
        merge_readiness["merge_ready"] = False
    return merge_readiness


async def targeted_fix_reasons(
    thread_id: str, exit_stage: dict[str, Any], state: dict[str, Any], provider: Any
) -> list[str]:
    """What the targeted-fix lever (graph.intake_node) sends the fixer: the blocking reasons exit
    finalize would write for the last attempt NOW -- _final_merge_readiness itself, so a report
    metrics-exit did not judge in that attempt contributes none of its prose, and gate-owned
    phrases are re-checked against the current tree. `exit_stage` is metrics-exit as the last
    attempt left it (before intake's reset clears last_verification). Recomputed rather than read
    from manifest.json's merge_readiness: a session finalized before that seed existed (c2bbdca1)
    still carries the stale "no main.ts"/"zero screenshots" prose there. NOT_RECHECKED_REASON is
    dropped -- "the recheck could not run" is nothing a code fix can address."""
    report = await _final_merge_readiness(
        thread_id,
        copy.deepcopy(exit_stage.get("approved_content") or {}),
        {**state, "stages": {**(state.get("stages") or {}), "metrics-exit": exit_stage}},
        provider,
    )
    return [
        str(r) for r in _presence_values(report.get("blocking_reasons"))
        if r != exit_readiness_checks.NOT_RECHECKED_REASON
    ]


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
    happen for it -- see the status logic below, which checks state["run_failure"] first.

    session_store.close_session is called EXACTLY ONCE, unconditionally, as the very last thing
    this function does -- never earlier. It used to run immediately after the PR was opened, before
    any of the report/changelog/commit work below it, which raced its own fire-and-forget container
    teardown (close_session -> end_session_container -> provider.terminate) against every
    still-in-flight sandbox call after it in this same function: the teardown task only gets a
    chance to run at this coroutine's next `await`, and this function has a dozen more of them
    (writes, then the final commit) before it actually finishes. That is the most likely mechanism
    behind a real incident where the DB/PR said "completed" but nothing landed on the branch --
    every write between the old close_session call and the final commit was racing a container that
    could vanish out from under it. Structurally impossible now: nothing here can call close_session
    before every sandbox-dependent write and the final commit have already resolved one way or the
    other (success in the try, or the degraded fallback in the except)."""
    run_id = state.get("run_id", "unknown")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merge_readiness = await _final_merge_readiness(thread_id, content, state, provider)
    terminal_failure = state.get("run_failure")

    # Status logic: merge_ready-aware, not just run_failure-aware -- a run that reaches exit but
    # fails a DETERMINISTIC gate (verify_exit_readiness forcing merge_ready=False: missing
    # screenshots, no test command, a metrics regression) must be recorded "failed" and stay
    # resumable, exactly like a hard crash. Getting this wrong would make an actually-unsuccessful
    # session permanently unresumable once resume is server-enforced against status=="completed".
    # Computed early (only depends on merge_readiness/terminal_failure, both already resolved above)
    # so the except branch below can downgrade it without duplicating this logic.
    merge_ready = bool(merge_readiness.get("merge_ready")) if merge_readiness else False
    if terminal_failure:
        status = "failed"
        failure_payload = terminal_failure
    elif merge_ready:
        status = "completed"
        failure_payload = None
    else:
        status = "failed"
        failure_payload = {
            "stage": "exit",
            "type": "gates_not_passed",
            "feedback": "; ".join(_presence_values((merge_readiness or {}).get("blocking_reasons"))) or "exit gates did not pass",
        }

    # Fetched unconditionally (not just on the completed branch, as before) so an existing PR url
    # survives being carried through to close_session even when this attempt's tail fails below --
    # pr_url is a direct SET in that UPDATE, never a COALESCE, so passing None there would silently
    # erase a real PR from a previous, successful finalize of this same (resumed) thread.
    session_row = await session_store.get_session(thread_id)
    pr_url = (session_row or {}).get("pr_url")

    try:
        spec_approval = await approvals.latest_approval(provider, thread_id, "specification")
        plan_approval = await approvals.latest_approval(provider, thread_id, "plan")
        raw_requirements = await repo_files.read_repo_file(
            provider, thread_id, workflow_persistence.RAW_REQUIREMENTS_APPROVED_PATH
        )
        raw_metrics = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/metrics-latest.json")
        metrics_summary = json.loads(raw_metrics) if raw_metrics else {}
        raw_hook_fail_opens = await repo_files.read_repo_file(provider, thread_id, repo_files.HOOK_FAIL_OPENS_PATH)
        hook_fail_opens = _parse_hook_fail_opens(raw_hook_fail_opens)
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
        # "Resolved" marking (user requirement 2026-09-10): only once this run's own merge_ready
        # is true -- stamp_delivery (metrics_compute_node) fires on a merely regression-clean run,
        # but verify_exit_readiness above can still force merge_ready=False afterward (missing
        # screenshots, no test command, unverified auth). Deliberately BEFORE us_ac_rows/the
        # snapshot write below, so this run's own exit report/snapshot reflects the just-stamped
        # state rather than stale pre-stamp state. Not scoped to this ticket's own AC ids -- see
        # stamp_resolution's own docstring.
        resolution_changed = merge_ready and spec_ledger.stamp_resolution(ledger_entries, run_id, timestamp)
        if resolution_changed:
            await spec_ledger.save_ledger(provider, thread_id, ledger_entries)
        # US/AC provenance rows: this run's own spec scope from STATE (already in hand -- no sandbox
        # read; the approved file equals it byte-for-byte), row set + carried-over from the ledger.
        own_spec = ((state.get("stages") or {}).get("specification") or {}).get("approved_content") or {}
        own_us_ids = {s.get("id") for s in (own_spec.get("user_stories") or []) if s.get("id")}
        own_ac_ids = spec_ledger.own_ac_ids_from_specification(own_spec)
        us_ac_rows = _us_ac_rows(ledger_entries, own_us_ids, own_ac_ids, run_id)
        carried_over = _undelivered_ac_ids(ledger_entries)

        # Traceability matrix, rebuilt here (not reused from metrics-report's own committed
        # traceability-matrix.md) because stamp_resolution just ran a few lines above -- an AC
        # resolved by THIS run's own exit gate must show as resolved in the SAME run's exit report,
        # not one run stale. Best-effort: a sandbox hiccup degrades this section, never the report.
        traceability_matrix_markdown = None
        try:
            sha_result = await provider.exec_in_sandbox(thread_id, "git rev-parse HEAD 2>/dev/null || true")
            commit_sha = (sha_result.stdout or "").strip() or None
            traceability_ac_resolution = spec_ledger.compute_ac_resolution(ledger_entries)
            traceability_rows = metrics_nodes._traceability_rows(
                ledger_entries, metrics_summary.get("ac_execution"),
                (session_row or {}).get("owner"), (session_row or {}).get("repo"), commit_sha,
            )
            traceability_matrix_markdown = metrics_nodes._render_traceability_matrix(
                traceability_rows, traceability_ac_resolution
            )
        except Exception:  # noqa: BLE001 -- degrade this section, never abort the report
            logger.warning("exit finalize: traceability matrix build failed for thread_id=%s", thread_id, exc_info=True)

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
            review_lines = _retired_scope_file_review(ledger_entries, diff["retired"])
            if review_lines:
                changelog_section.append(
                    "- **Review & remove if unused** (production code cleanup is best-effort, not "
                    "automatically deleted -- test files referencing a retired id are already "
                    "gated/removed):"
                )
                changelog_section.extend(f"  {line}" for line in review_lines)
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

        # Ruling 8, Part B: refresh the regression baseline from this run's own final scan -- only on
        # the completed branch above, see _baseline_refresh_payload's own docstring for why.
        baseline_payload = _baseline_refresh_payload(status, metrics_summary)
        if baseline_payload is not None:
            await repo_files.write_repo_file(provider, thread_id, repo_scan.BASELINE_PATH, baseline_payload)

        # Per-run exit report artifacts (durable even once the session ages out of the UI's recent
        # list): the raw diff/log this run actually produced, plus the same metrics/delta numbers
        # the frontend Report tab shows live, frozen at exit time so a past session's report page
        # can render identically. Each of these three reads is best-effort: a sandbox hiccup on any
        # ONE of them must degrade that section, not abort the whole report (same fail-open contract
        # as _scan_regression_reasons, agent/src/rebuild.py).
        try:
            files_changed_stat, commits_log = await _files_changed(provider, thread_id, state.get("run_baseline_commit"))
        except Exception:  # noqa: BLE001 -- degrade this section, never abort the report
            logger.warning("exit finalize: _files_changed failed for thread_id=%s", thread_id, exc_info=True)
            files_changed_stat, commits_log = "", ""
        try:
            screenshots = await _list_screenshots(provider, thread_id, run_id)
        except Exception:  # noqa: BLE001 -- degrade this section, never abort the report
            logger.warning("exit finalize: _list_screenshots failed for thread_id=%s", thread_id, exc_info=True)
            screenshots = []
        delta_summary = repo_scan.delta_summary(metrics_summary.get("repo_scan_delta"))
        try:
            ledger_rows = await _load_ledger_rows(provider, thread_id)
        except Exception:  # noqa: BLE001 -- degrade this section, never abort the report
            logger.warning("exit finalize: _load_ledger_rows failed for thread_id=%s", thread_id, exc_info=True)
            ledger_rows = []
        divergence_rows, divergence_section = _divergence_ledger(
            [r for r in ledger_rows if r.get("node") == "divergence_snapshot" and r.get("run_id") == run_id]
        )
        # Deterministic reconciliation -- see _reconcile_divergence_risk_notes's own docstring.
        reconciled_risk_notes = _reconcile_divergence_risk_notes(
            divergence_rows, _presence_values(merge_readiness.get("risk_notes"))
        )
        if reconciled_risk_notes != _presence_values(merge_readiness.get("risk_notes")):
            merge_readiness["risk_notes"] = _presence_from_values(
                reconciled_risk_notes,
                empty_reason="unreachable: at least one note is always appended in this branch",
            )
        stage_rows, stage_section = _stage_summary(ledger_rows, state.get("stages"), terminal_failure)
        # Remediation's approved report: known_gaps become the findings table's "known gap: <reason>"
        # dispositions, findings_addressed the "fixed by remediation" count. {} when remediation never
        # approved (escalated runs) -- the renderer degrades to the deterministic disposition classes.
        # (No function-local `from . import metrics_nodes` here: it made the name local to this
        # whole function, so the traceability-matrix block above raised UnboundLocalError -- caught
        # by its own except -- and that section silently never rendered. The module import serves.)
        try:
            remediation_report = await metrics_nodes.read_remediation_report(provider, thread_id)
        except Exception:  # noqa: BLE001 -- report rendering must survive an unreadable artifact
            remediation_report = {}
        fallback_metrics: dict[str, Any] | None = None
        if not metrics_summary:
            # Escalated runs skip metrics_compute; surface what already exists instead of nothing.
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

        # One render, three destinations (the two committed exit-markdown copies below, plus the PR
        # body further down) -- they differ only in how a screenshot path resolves to an image, so
        # screenshot_prefix is the only thing that varies per call.
        def _full_markdown(screenshot_prefix: str) -> str:
            return (
                render_exit_markdown(merge_readiness or {}) + "\n" + _render_history_sections(
                    files_changed_stat=files_changed_stat,
                    commits_log=commits_log,
                    metrics_summary=metrics_summary,
                    delta_summary=delta_summary,
                    screenshots=screenshots,
                    run_id=run_id,
                    e2e=state.get("e2e"),
                    screenshot_prefix=screenshot_prefix,
                    stages=state.get("stages"),
                    us_ac_rows=us_ac_rows,
                    carried_over=carried_over,
                    fallback_metrics=fallback_metrics,
                    remediation=remediation_report,
                    traceability_matrix_markdown=traceability_matrix_markdown,
                    hook_fail_opens=hook_fail_opens,
                )
                + ("\n" + failure_section if failure_section else "")
                + ("\n" + divergence_section if divergence_section else "")
            )

        exit_markdown = _full_markdown("./")
        await repo_files.write_repo_file(provider, thread_id, exit_md_path, exit_markdown)

        # A second copy at a FIXED, obvious path. The per-run file above is the archive, but its name
        # carries a run id and sits a directory deep, so on a delivered branch nobody finds it -- the
        # report was reviewed as "missing" for five consecutive runs while being committed every time.
        # Screenshot links are re-based to history/... because this copy lives one level up from them.
        latest_markdown = _full_markdown("history/")
        await repo_files.write_repo_file(provider, thread_id, EXIT_REPORT_PATH, latest_markdown)
        # The numbered stage artifact carries the SAME full report. This node is its only writer --
        # metrics-exit's StageSpec sets render_markdown=None precisely so the generic persist can't
        # revert this file to the 4-section stub at the start of the next run.
        await repo_files.write_repo_file(provider, thread_id, workflow_persistence.METRICS_EXIT_MD_PATH, latest_markdown)

        commit_targets = [MANIFEST_PATH, HISTORY_DIR, CHANGELOG_PATH, EXIT_REPORT_PATH, workflow_persistence.METRICS_EXIT_MD_PATH]
        if baseline_payload is not None:
            commit_targets.append(repo_scan.BASELINE_PATH)
        if resolution_changed:
            commit_targets.append(spec_ledger.LEDGER_PATH)
        await git_ops.commit_paths(
            provider,
            thread_id,
            commit_targets,
            "ai-dev-workflow: exit finalize (manifest, changelog, exit report)",
        )

        # PR create/update, moved to AFTER the commit above (it used to fire before any of this run's
        # own artifacts were written, so its body was always the model's bare freeform prose) -- the
        # body now carries the same full report exit.md does, screenshot links re-based to absolute
        # raw.githubusercontent.com URLs since a PR description isn't a committed file GitHub can
        # resolve a relative image path against.
        if status == "completed" and session_row:
            token = git_ops.get_push_token(thread_id)
            if token:
                pr_screenshot_prefix = (
                    f"https://raw.githubusercontent.com/{session_row['owner']}/{session_row['repo']}/"
                    f"{session_row['work_branch']}/{HISTORY_DIR}/"
                )
                pr_body = _full_markdown(pr_screenshot_prefix)
                if pr_url:
                    # Idempotent re-finalize (hydrate-short-circuit path, e.g. a resumed thread whose
                    # exit stage was already approved): never open a second PR, but DO refresh the
                    # existing one's body so it reflects this attempt's content instead of staying
                    # frozen at whatever the first finalize wrote.
                    await git_ops.update_pull_request(
                        owner=session_row["owner"], repo=session_row["repo"], pr_url=pr_url, body=pr_body, token=token,
                    )
                else:
                    pr_url = await git_ops.open_pull_request(
                        owner=session_row["owner"],
                        repo=session_row["repo"],
                        source_branch=session_row["source_branch"],
                        work_branch=session_row["work_branch"],
                        title=merge_readiness.get("pr_title") or f"ai-dev-workflow: {run_id}",
                        body=pr_body,
                        token=token,
                    )
            else:
                logger.warning("no push token retained for thread_id=%s -- skipping PR create/update", thread_id)
    except Exception as exc:  # noqa: BLE001 -- this is the guarantee: exit.md must exist either way
        # Something got through every inner guard above (most likely the final commit_paths itself).
        # Downgrade in place and best-effort write a MINIMAL report instead of leaving nothing at all
        # -- a human reading a degraded "generation failed" exit.md is the whole point of this stage;
        # silence is the one outcome that must never happen.
        logger.error("exit finalize: report generation failed for thread_id=%s -- writing degraded report", thread_id, exc_info=True)
        reason = f"exit report generation failed: {exc}"
        existing_reasons = _presence_values(merge_readiness.get("blocking_reasons"))
        merge_readiness["blocking_reasons"] = _presence_from_values(
            [reason, *existing_reasons] if reason not in existing_reasons else existing_reasons,
            empty_reason="unreachable: reason is always appended in this branch",
        )
        merge_readiness["merge_ready"] = False
        merge_ready = False
        status = "failed"
        failure_payload = {"stage": "exit", "type": "exit_report_failed", "feedback": reason}
        try:
            await preflight_nodes.update_manifest(provider, thread_id, {"merge_readiness": merge_readiness})
            degraded_md = render_exit_markdown(merge_readiness)
            await repo_files.write_repo_file(provider, thread_id, f"{HISTORY_DIR}/{run_id}-exit.md", degraded_md)
            await repo_files.write_repo_file(provider, thread_id, EXIT_REPORT_PATH, degraded_md)
            await repo_files.write_repo_file(provider, thread_id, workflow_persistence.METRICS_EXIT_MD_PATH, degraded_md)
            await repo_files.write_repo_file(
                provider, thread_id, f"{HISTORY_DIR}/{run_id}-report.json",
                json.dumps(
                    {"run_id": run_id, "timestamp": timestamp, "merge_readiness": merge_readiness, "run_failure": terminal_failure},
                    indent=2, default=str,
                ) + "\n",
            )
            await git_ops.commit_paths(
                provider,
                thread_id,
                [MANIFEST_PATH, HISTORY_DIR, EXIT_REPORT_PATH, workflow_persistence.METRICS_EXIT_MD_PATH],
                "ai-dev-workflow: exit finalize (degraded -- report generation failed)",
            )
        except Exception:  # noqa: BLE001 -- sandbox is truly unreachable; nothing more to do locally
            logger.error("exit finalize: degraded fallback write ALSO failed for thread_id=%s", thread_id, exc_info=True)

    # Unconditional, exactly once, last -- see this function's own docstring for why.
    await session_store.close_session(
        thread_id,
        run_id=run_id,
        status=status,
        failure=failure_payload,
        merge_ready=merge_ready if merge_readiness else None,
        pr_title=(merge_readiness or {}).get("pr_title"),
        pr_url=pr_url,
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
    # _presence_values/_presence_from_values: the read-modify-rewrite helpers verify_exit_readiness
    # and exit_finalize_node use to keep blocking_reasons/risk_notes PresenceList-shaped after the
    # model's initial (already-typed) output -- never leave values populated while status='absent'.
    assert _presence_values({"status": "present", "values": ["a", "b"], "reason": ""}) == ["a", "b"]
    assert _presence_values({"status": "absent", "values": [], "reason": "none found"}) == []
    assert _presence_values(["legacy", "bare", "list"]) == ["legacy", "bare", "list"]
    assert _presence_values(None) == []
    assert _presence_from_values(["x"], empty_reason="unused") == {
        "status": "present", "values": ["x"], "reason": "",
    }
    assert _presence_from_values([], empty_reason="nothing to report") == {
        "status": "absent", "values": [], "reason": "nothing to report",
    }

    # _targeted_fix_unresolved_problems / GATE_OWNED_REASON_MARKERS / _manifest_completeness_topic /
    # _combined_test_command_from_apps / evaluate_merge_readiness all moved to
    # gates/exit_readiness_checks.py (2026-09-30, Task 14) -- their own assertions now live in that
    # module's self-check (`cd agent && uv run python -m src.gates.exit_readiness_checks`). One
    # integration-only assertion here proves this module's import/wiring is actually live, not just
    # that the (already-tested) pure function works in isolation.
    stale_metrics_reason = "metrics were not recorded for this run -- the regression gate never passed"
    assert exit_readiness_checks.targeted_fix_unresolved_problems({"run_id": "r1", "reasons": ["still open"]}, "r1") == ["still open"]
    assert any(marker in stale_metrics_reason for marker in exit_readiness_checks.GATE_OWNED_REASON_MARKERS), (
        "this deterministic phrase must be gate-owned so a stale copy is droppable"
    )

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

    # Wireframe-coverage line (e2e_nodes.check_wireframe_coverage's stamped fields): a fully-covered
    # run states the parity plainly; total=None (nothing to check -- no wireframes in the plan)
    # renders no line at all rather than a misleading "0/0". This is the fix for the exact bug this
    # feature shipped for: a raw screenshot-file count (14) silently read as if it meant the same
    # thing as the plan's wireframe count (6).
    full_coverage = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1",
        e2e={"status": "passed", "wireframe_coverage_total": 6, "wireframe_coverage_missing": []},
    )
    assert "**Wireframe coverage**: 6/6 wireframed screens verified in e2e." in full_coverage, full_coverage
    assert "Missing:" not in full_coverage

    partial_coverage = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1",
        e2e={
            "status": "passed", "wireframe_coverage_total": 6,
            "wireframe_coverage_missing": [{"screen": "poll-not-found", "ac_ids": ["US-0006.3"]}],
            "unwireframed_screens": ["US-9999-9"],
        },
    )
    assert "**Wireframe coverage**: 5/6 wireframed screens verified in e2e." in partial_coverage, partial_coverage
    assert "Missing: poll-not-found (US-0006.3)." in partial_coverage, partial_coverage
    assert "**Additional e2e evidence with no matching wireframe** (advisory, not blocking): US-9999-9." in partial_coverage

    not_applicable = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1", e2e={"status": "passed", "wireframe_coverage_total": None},
    )
    assert "Wireframe coverage" not in not_applicable, not_applicable

    # --- score explanations: every Metrics Bar number gets a "how we got this" line -------------
    score_metrics_fixture = {
        "code_health_score": 88, "code_health_subscores": {"security": 90.0, "complexity": None},
        "app_health_score": 76.5, "app_health_inputs": {"coverage_fraction": 0.9, "pass_rate_fraction": 0.63},
        "ac_resolution": {"score": 80.0, "resolved": 4, "total": 5, "deferred_excluded": 1, "deferred_after_coding": 1},
        "productivity_estimate": {
            "added_lines": 300, "mean_ccn": 4.2, "complexity_multiplier": 1.0,
            "ac_resolution_discount": 80.0, "review_overhead_fraction": 0.175, "estimated_hours_saved": 4.2,
        },
    }
    explained = "\n".join(_render_score_explanations(score_metrics_fixture))
    assert "Code Health (88)" in explained and "security=90.0" in explained and "complexity=" not in explained
    assert "App Health (76.5)" in explained and "90.0%" in explained and "63.0%" in explained
    assert "AC Resolution (80.0%)" in explained and "4 of 5 ACs resolved" in explained
    assert "1 AC(s) were deferred AFTER coding" in explained
    assert "~4.2 hours" in explained and "Capability-Based Lifecycle Benchmarking" in explained
    assert _render_score_explanations(None)[0] == "### How these scores were calculated", "must never raise on absent metrics_summary"

    # B4 (tech-stack startability pivot): a non-startable app's App Health block reads
    # "unavailable -- app not startable: <reason>" instead of the normal coverage/pass-rate blend
    # wording, even though app_health_score is None the same way an unmeasured one would be --
    # metrics_nodes.metrics_compute_node forces app_health_score=None and stamps this reason
    # whenever tech_stack.get("startable", True) is False.
    not_startable_fixture = dict(score_metrics_fixture)
    not_startable_fixture["app_health_score"] = None
    not_startable_fixture["app_health_inputs"] = {
        "coverage_fraction": 0.9, "pass_rate_fraction": 0.63,
        "not_startable_reason": "backend candidate never opened its port within 45s",
    }
    not_startable_explained = "\n".join(_render_score_explanations(not_startable_fixture))
    assert "App Health (--)" in not_startable_explained
    assert "unavailable -- app not startable: backend candidate never opened its port within 45s" in not_startable_explained
    assert "blend of test coverage" not in not_startable_explained, (
        "the not-startable branch must replace the coverage/pass-rate wording, not sit alongside it"
    )

    with_matrix = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1", traceability_matrix_markdown="# Acceptance Criteria Traceability Matrix\n\n| AC |\n|---|\n",
    )
    assert "# Acceptance Criteria Traceability Matrix" in with_matrix
    without_matrix = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None, screenshots=[], run_id="r1",
    )
    assert "Traceability Matrix" not in without_matrix

    # --- scan sections: score table, findings dispositions, tool table --------------------------
    scan_fixture = {
        "summary": {
            "health_score": 71, "health_raw": 71.1, "health_coverage_multiplier": 1.0,
            "health_coverage_fraction": 1.0, "active_critical_count": 0, "kloc": 50.92,
            "health_subscores": {"security": 37.1, "coverage": 95.0, "dependencies": None},
            "health_weights_used": {"security": 0.45, "coverage": 0.55},
            "health_basis": {"security": "7 finding(s), 56.0 risk units / 50.9 kloc"},
        },
        "findings": [
            {"id": "aaa111aaa111", "severity": "high", "category": "vulnerability",
             "tools": ["osv-scanner", "trivy"], "rule_id": "CVE-2026-68945",
             "title": "Angular: Cross-Request Response Reuse | pipes",  # the | must be escaped
             "location": {"path": "src/web/package-lock.json", "start_line": None},
             "gating": True, "actionable": True},
            {"id": "bbb222bbb222", "severity": "low", "category": "sast",
             "tools": ["semgrep"], "rule_id": "typescript.i18next.jsx-not-internationalized",
             "title": "not internationalized", "location": {"path": "src/web/app/page.tsx", "start_line": 4},
             "gating": False, "actionable": False},
            {"id": "ccc333ccc333", "severity": "low", "category": "sast", "tools": ["eslint-security"],
             "rule_id": "security/detect-object-injection", "title": "object injection sink",
             "location": {"path": "src/web/lib/q.ts", "start_line": 42}, "gating": False, "actionable": True},
        ],
        "tools": [
            {"name": "trivy", "version": "Version: 0.74.0", "status": "ok", "duration_ms": 69180, "findings": 7, "notes": ""},
            {"name": "interrogate", "version": None, "status": "not_applicable", "duration_ms": 3,
             "findings": 0, "notes": "No applicable files detected"},
        ],
    }
    # Task 11: findings_addressed/known_gaps are real PresenceList-shaped dicts here, not a bare
    # list[str] -- proves _render_scan_sections/_finding_disposition read the ACTUAL production
    # shape correctly, not just the legacy bare list _presence_values also tolerates.
    remediation_fixture = {
        "findings_addressed": {"status": "present", "values": ["ddd444ddd444"], "reason": ""},
        "known_gaps": {
            "status": "present",
            "values": ["aaa111aaa111: no fixed version published upstream yet"],
            "reason": "",
        },
    }
    scan_md = "\n".join(_render_scan_sections(scan_fixture, remediation_fixture))
    assert "## Health score" in scan_md and "**71 / 100**" in scan_md and "raw 71.1" in scan_md, scan_md
    assert "| security | 45% | 37.1 | 7 finding(s), 56.0 risk units / 50.9 kloc |" in scan_md, scan_md
    assert "| dependencies | -- | -- | unmeasured, weight redistributed |" in scan_md, scan_md
    assert "## Findings (3 clusters)" in scan_md
    assert "known gap: no fixed version published upstream yet" in scan_md, "the recorded reason must render"
    assert "advisory rule" in scan_md, "auto-exempt classes get deterministic reasons"
    assert "open -- introduced after remediation" in scan_md, "an unexplained actionable finding is labeled open"
    assert "1 fixed by remediation this run" in scan_md and "1 known gap(s)" in scan_md and "1 open" in scan_md, scan_md
    assert "Reuse \\| pipes" in scan_md, "pipe characters must not break the table"
    assert "osv-scanner, trivy" in scan_md, "the Tool(s) column names every corroborating source"
    assert "## Scanner tools (2)" in scan_md and "| NOT_APPLICABLE | 0.0s | 0 | No applicable files detected |" in scan_md, scan_md
    assert "| trivy | Version: 0.74.0 | OK | 69.2s | 7 |" in scan_md, scan_md
    unavailable = _render_scan_sections(None, None)
    assert unavailable[0] == "## Health score" and "unavailable" in unavailable[2], unavailable

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
    # Status column: real stage status for a row the caller's `stages` dict knows about ("plan" was
    # passed in as "approved"), "-" for a ledger-only tag with no real-stage counterpart ("e2e" here
    # since the fixture's `stages` dict never mentions it).
    assert by_stage["plan"]["status"] == "approved", by_stage["plan"]
    assert by_stage["e2e"]["status"] == "-", by_stage["e2e"]
    assert "| specification | - | 2:20 | 2 | 200/100 | $1.00 |" in stage_md, stage_md
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

    # _reconcile_divergence_risk_notes: an open row is force-included in risk_notes regardless of
    # what the model already wrote there -- this is the fix for the exact contradiction above
    # (report claims "fixed", ledger says open). A closed-only ledger changes nothing.
    reconciled = _reconcile_divergence_risk_notes(rows, ["unrelated pre-existing note"])
    assert "unrelated pre-existing note" in reconciled, reconciled
    assert any("US-0002.1" in n and "still OPEN" in n for n in reconciled), reconciled
    assert not any("Plan Step 4" in n for n in reconciled), reconciled  # closed rows are not notes
    # Idempotent: reconciling again against its own output must not duplicate the note.
    assert _reconcile_divergence_risk_notes(rows, reconciled) == reconciled, reconciled
    assert _reconcile_divergence_risk_notes([], ["kept"]) == ["kept"]

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

    # Regression lock for the browser/runner-missing infra gap (income-investor thread f0fef8ba,
    # 2026-09-26): this used to render as a benign "skipped" here (and, worse, route as an
    # indistinguishable-from-green "pass" in make_e2e_route_after_run) -- it must now always render
    # as a real failure, never the word "skipped", regardless of no production change being needed
    # in THIS file (the failed_tests-truthiness check below is already generic).
    browser_missing_escalated = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None, screenshots=[], run_id="r1",
        e2e={
            "status": "failed", "cannot_verify": True, "total": 0,
            "failed_tests": [{
                "title": "e2e suite",
                "error": "playwright browser executable is missing in this environment",
            }],
        },
    )
    assert "- **E2E**: failed (1/0 failed)" in browser_missing_escalated, browser_missing_escalated
    assert "(none captured -- e2e failed)" in browser_missing_escalated
    assert "skipped" not in browser_missing_escalated.lower()

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

    # _parse_hook_fail_opens / _render_hook_fail_opens_section: the file's own common case (absent,
    # per repo_files.read_repo_file's None-on-missing contract) must add NOTHING to the report --
    # this is the one section allowed to render no heading at all when empty.
    assert _parse_hook_fail_opens(None) == []
    assert _parse_hook_fail_opens("") == []
    assert _render_hook_fail_opens_section(None) == []
    assert _render_hook_fail_opens_section([]) == []

    _fixture_jsonl = (
        '{"ts": "2026-09-29T00:00:00Z", "hook": "check-narrative-format-stop", '
        '"stage": "specification", "reason": "missing python3"}\n'
        "not valid json, skipped\n"
        '{"ts": "2026-09-29T00:00:01Z", "hook": "check-coverage-stop", '
        '"stage": "minimal-code-to-green", "reason": "unparsable coverage_parsing.py output"}\n'
        "\n"
        '{"ts": "2026-09-29T00:00:02Z", "hook": "check-quick-scan-stop", '
        '"stage": "minimal-code-to-green", "reason": "bandit unavailable, errored, or timed out"}\n'
    )
    _fail_opens = _parse_hook_fail_opens(_fixture_jsonl)
    assert len(_fail_opens) == 3, "the one malformed/blank line must be skipped, not fatal to the rest"
    assert [fo["hook"] for fo in _fail_opens] == [
        "check-narrative-format-stop", "check-coverage-stop", "check-quick-scan-stop",
    ]

    _fail_open_lines = _render_hook_fail_opens_section(_fail_opens)
    assert _fail_open_lines[0] == "## Deterministic checks skipped this session"
    _fail_open_text = "\n".join(_fail_open_lines)
    assert "3 deterministic check(s)" in _fail_open_text, _fail_open_text
    assert "check-narrative-format-stop` (specification, missing python3)" in _fail_open_text, _fail_open_text
    assert "check-coverage-stop` (minimal-code-to-green, unparsable coverage_parsing.py output)" in _fail_open_text
    assert "check-quick-scan-stop` (minimal-code-to-green, bandit unavailable, errored, or timed out)" in _fail_open_text

    # And through the full report renderer: absent file -> no section at all (no stray heading);
    # present file -> the section actually shows up in the assembled markdown.
    _no_fail_opens_report = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1", hook_fail_opens=None,
    )
    assert "Deterministic checks skipped" not in _no_fail_opens_report
    _with_fail_opens_report = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None,
        screenshots=[], run_id="r1", hook_fail_opens=_fail_opens,
    )
    assert "## Deterministic checks skipped this session" in _with_fail_opens_report
    assert "check-narrative-format-stop" in _with_fail_opens_report

    # _us_ac_rows / _undelivered_ac_ids / _render_us_ac_section: US/AC provenance in the exit
    # report -- own-spec scope + changed entries + delivered-this-run entries, flake-ticket
    # synthetic stories filtered, US coded/tested aggregated from children.
    ledger = [
        {"id": "US-0001", "kind": "user_story", "status": "active", "title": "Counter",
         "first_seen_run_id": "r1", "last_revised_run_id": "r1"},
        {"id": "US-0001.1", "kind": "acceptance_criterion", "parent_us_id": "US-0001",
         "status": "active", "description": "Increments", "first_seen_run_id": "r1",
         "last_revised_run_id": "r1", "coded_run_id": "r1", "coded_at": "t1",
         "tested_run_id": "r1", "tested_at": "t1", "test_ids": ["[US-0001.1] increments"],
         "ui_related": True},
        {"id": "US-0001.2", "kind": "acceptance_criterion", "parent_us_id": "US-0001",
         "status": "revised", "description": "Shows doubled value", "first_seen_run_id": "r1",
         "last_revised_run_id": "r2", "coded_run_id": "r2", "coded_at": "t2",
         "tested_run_id": "r2", "tested_at": "t2", "test_ids": ["[US-0001.2] doubles"],
         "ui_related": False},
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
    assert by_id["US-0001.1"]["ui_related"] is True and by_id["US-0001.2"]["ui_related"] is False, by_id
    assert by_id["US-0002.1"]["ui_related"] is None, "a ledger entry that predates ui_related must not fabricate a value"
    assert _undelivered_ac_ids(ledger) == ["US-0004.1"], _undelivered_ac_ids(ledger)
    section = "\n".join(_render_us_ac_section(rows, _undelivered_ac_ids(ledger), "r2"))
    assert "## User stories & acceptance criteria this run" in section
    assert "| US-0001.1 | unchanged | Increments | Yes |" in section, section
    assert "| US-0001.2 | modified | Shows doubled value | No |" in section and "r2 (this run)" in section, section
    assert "| US-0002.1 | deleted | Resets | -- |" in section, section
    assert "Carried over -- not delivered**: US-0004.1" in section
    assert "(none recorded" in "\n".join(_render_us_ac_section([], [], "r2"))

    # Task 13b: METRICS_EXIT_HARD_RULES -- one line per real merge-blocking condition in
    # verify_exit_readiness (see the constant's own comment for the count breakdown).
    assert len(METRICS_EXIT_HARD_RULES) == 9, len(METRICS_EXIT_HARD_RULES)
    assert all(isinstance(r, str) and r.strip() for r in METRICS_EXIT_HARD_RULES)

    # _combined_test_command_from_apps / _manifest_completeness_topic: moved to
    # gates/exit_readiness_checks.py (Task 14) -- their full assertions live in that module's own
    # self-check now.

    # verify_exit_readiness per-sub-check rows (advisory, never failed; passed stays True), with
    # the sandbox reads stubbed. A complete manifest means no re-scan / update_manifest call.
    import asyncio

    from .graph import TARGETED_FIX_UNRESOLVED_PATH
    from .schemas import TECH_STACK_DRAFT_EXAMPLE

    def _run_exit_verify(files: dict[str, Any], screenshots: list[str], strict_log: bool) -> Any:
        async def _fake_read(_provider: Any, _thread_id: str, path: str) -> str | None:
            return json.dumps(files[path]) if path in files else None

        async def _fake_screens(_provider: Any, _thread_id: str, _run_id: str) -> list[str]:
            return screenshots

        original_read, original_screens = repo_files.read_repo_file, globals()["_list_screenshots"]
        repo_files.read_repo_file = _fake_read  # type: ignore[assignment]
        globals()["_list_screenshots"] = _fake_screens
        try:
            log = CheckLog("metrics-exit_verify", VERIFY_CHECKS, strict=True) if strict_log else None
            return asyncio.run(verify_exit_readiness("t-exit", {}, "r1", None, object(), "", 0, log=log))
        finally:
            repo_files.read_repo_file = original_read  # type: ignore[assignment]
            globals()["_list_screenshots"] = original_screens

    full_manifest = {"app_check": {"apps": [{"name": "web"}]}, "test_command": "npm test", "coverage_commands": [{"x": 1}]}
    ui_stale = _run_exit_verify({
        MANIFEST_PATH: full_manifest,
        workflow_persistence.TECH_STACK_APPROVED_PATH: {
            **TECH_STACK_DRAFT_EXAMPLE.tech_stack.model_dump(mode="json"), "frameworks": ["Next.js"],
        },
        ".ai-dev-workflow/metrics-latest.json": {"run_id": "an-older-run"},
    }, [], strict_log=True)
    assert ui_stale.passed
    assert [(r["id"], r["status"]) for r in ui_stale.checks] == [
        (EXIT_MANIFEST.id, "passed"), (EXIT_SCREENSHOTS.id, "advisory"), (EXIT_METRICS.id, "advisory"),
        (EXIT_TARGETED_FIX.id, "skipped"), (EXIT_AUTH.id, "skipped"),
    ], ui_stale.checks
    assert len(ui_stale.report["blockers"]) == 2 and "never passed" in ui_stale.checks[2]["detail"]

    api_clean = _run_exit_verify({
        MANIFEST_PATH: full_manifest,
        ".ai-dev-workflow/metrics-latest.json": {"run_id": "r1", "regression_gate": {"reasons": []}},
        TARGETED_FIX_UNRESOLVED_PATH: {"run_id": "r1", "reasons": ["coverage below threshold 60%"]},
    }, [], strict_log=False)  # no log kwarg: exit_finalize_node's own call shape
    assert [(r["id"], r["status"]) for r in api_clean.checks] == [
        (EXIT_MANIFEST.id, "passed"), (EXIT_SCREENSHOTS.id, "skipped"), (EXIT_METRICS.id, "passed"),
        (EXIT_TARGETED_FIX.id, "advisory"), (EXIT_AUTH.id, "skipped"),
    ], api_clean.checks
    assert api_clean.report["blockers"] == ["coverage below threshold 60%"]

    # Phrase audit (session c2bbdca1): EVERY deterministic phrase a blocker can carry -- produced by
    # the REAL producers, not retyped here -- must be dropped by the stale filter when this run's
    # checks no longer raise it. A new phrase nobody added to GATE_OWNED_REASON_MARKERS (or a
    # manifest topic) fails here instead of silently re-blocking every later attempt. Targeted-fix
    # reasons are deliberately absent: they are verbatim copies of earlier blocking reasons (no
    # vocabulary of their own), owned by whichever phrase they copy.
    from . import metrics_nodes as _metrics_nodes
    from .gates import readme_gate

    erc = exit_readiness_checks
    critical_tool = workflow_config.AIDW_SECURITY_CRITICAL_TOOL_NAMES[0]
    regressed = {"direction": "regressed", "delta": -50, "from": 90, "to": 40}
    every_regression = dict(
        delta_summ={"metrics": {"coverage_line_rate": regressed, "coverage_branch_rate": regressed, "health_score": regressed}},
        baseline_has_findings=True, ac_verification={"total": 3}, ac_execution=None, is_ui_app=True,
    )
    dirty_scan = {"gating_count": 2, "severity_floor": "high", "degraded": [critical_tool], "measures": {"duplication_percent": 99.0}}
    regression_phrases = [
        *_metrics_nodes.regression_reasons(dirty_scan, coverage={"line_rate": 1.0, "branch_rate": 1.0}, **every_regression),
        *_metrics_nodes.regression_reasons(dirty_scan, coverage={}, **every_regression),
    ]
    readme_phrases = [
        *readme_gate.readme_problems(None),
        *readme_gate.readme_problems("no title\n\n## License\n\n## Notes\n"),
    ]
    unverified_auth = {"app_auth": {"auth_mode": "required", "secrets_present": True}, "e2e": {"status": "failed"}}
    deterministic_phrases = [
        *erc.manifest_presence_problems({}),
        *erc.screenshot_problems(True, 0),
        *erc.metrics_problems({}, "r1")[0],
        *erc.auth_problems(unverified_auth, True)[0],
        *erc.auth_problems({**unverified_auth, "e2e": {"auth_check": {"passed": False}}}, True)[0],
        *regression_phrases,
        *readme_phrases,
        _terminal_failure_reason({"stage": "r_x", "type": "rebuild_cap_exceeded", "feedback": "boom"}),
        "exit report generation failed: boom",  # exit_finalize_node's degraded branch (f-string there)
        erc.NOT_RECHECKED_REASON,
    ]
    assert len(regression_phrases) == 18 and len(readme_phrases) == 5, (regression_phrases, readme_phrases)
    for phrase in deterministic_phrases:
        verdict = erc.evaluate_merge_readiness([], {"status": "present", "values": [phrase], "reason": ""}, False)
        assert verdict["stale_reasons"] == [phrase], f"deterministic phrase is not stale-filterable: {phrase!r}"

    # _final_merge_readiness: prior-attempt report text vs this attempt's own (session c2bbdca1).
    prior_prose = "No runnable application exists: apps/web has no main.ts"
    prior_report = {
        "merge_ready": False,
        "blocking_reasons": {"status": "present", "values": [prior_prose, erc.NO_SCREENSHOTS_REASON], "reason": ""},
        "pr_title": "WIP: scaffold only, NOT ready to merge",
        "pr_description_markdown": "Build stopped mid-way.",
        "risk_notes": {"status": "present", "values": ["zero e2e screenshots exist"], "reason": ""},
    }
    ui_tech_stack = {**TECH_STACK_DRAFT_EXAMPLE.tech_stack.model_dump(mode="json"), "frameworks": ["Angular"]}
    rebuild_failure = {"stage": "r_adversarial_compliance", "type": "rebuild_cap_exceeded", "feedback": "scan-delta gate"}

    def _run_final(
        state: dict[str, Any], content: dict[str, Any], files: dict[str, Any], *, crash: bool = False,
        exit_stage: dict[str, Any] | None = None,
    ) -> Any:
        """_final_merge_readiness, or targeted_fix_reasons when `exit_stage` (pre-reset) is given."""
        async def _fake_read(_provider: Any, _thread_id: str, path: str) -> str | None:
            if crash:
                raise RuntimeError("container gone")
            return json.dumps(files[path]) if path in files else None

        async def _fake_screens(_provider: Any, _thread_id: str, _run_id: str) -> list[str]:
            return [".ai-dev-workflow/history/r1-screens/001-home.png"]

        original_read, original_screens = repo_files.read_repo_file, globals()["_list_screenshots"]
        repo_files.read_repo_file = _fake_read  # type: ignore[assignment]
        globals()["_list_screenshots"] = _fake_screens
        try:
            if exit_stage is not None:
                return asyncio.run(targeted_fix_reasons("t-exit", {**exit_stage, "approved_content": content}, state, object()))
            return asyncio.run(_final_merge_readiness("t-exit", content, state, object()))
        finally:
            repo_files.read_repo_file = original_read  # type: ignore[assignment]
            globals()["_list_screenshots"] = original_screens

    no_metrics_files = {MANIFEST_PATH: full_manifest, workflow_persistence.TECH_STACK_APPROVED_PATH: ui_tech_stack}
    resumed = {"run_id": "r1", "run_failure": rebuild_failure, "stages": {"metrics-exit": {"last_verification": None}}}
    judged = {**resumed, "stages": {"metrics-exit": {"last_verification": {"passed": True}}}}
    assert not metrics_exit_judged_this_attempt(resumed) and metrics_exit_judged_this_attempt(judged)
    assert not metrics_exit_judged_this_attempt({})

    # Not judged this attempt: none of the prior prose survives -- only the terminal failure plus
    # what this attempt's checks raised (metrics never recorded; screenshots exist now).
    prior_copy = json.loads(json.dumps(prior_report))
    stale = _run_final(resumed, prior_copy, no_metrics_files)
    assert stale["blocking_reasons"]["values"] == [_terminal_failure_reason(rebuild_failure), erc.metrics_problems({}, "r1")[0][0]], stale
    assert stale["merge_ready"] is False and stale["pr_title"] == "ai-dev-workflow: r1" and stale["risk_notes"]["status"] == "absent"
    assert prior_copy == prior_report, "the prior approved_content itself is never mutated on the not-judged path"
    # Judged this attempt: the model's own prose is this attempt's and stays; only the stale
    # deterministic phrase (screenshots, now present) is dropped -- unchanged behaviour.
    kept = _run_final(judged, json.loads(json.dumps(prior_report)), no_metrics_files)
    assert prior_prose in kept["blocking_reasons"]["values"] and erc.NO_SCREENSHOTS_REASON not in kept["blocking_reasons"]["values"], kept
    assert kept["pr_title"] == prior_report["pr_title"]
    # Not judged, everything clean, no terminal failure: the seed's placeholder clears -> ready.
    clean_files = {**no_metrics_files, ".ai-dev-workflow/metrics-latest.json": {"run_id": "r1", "regression_gate": {"reasons": []}}}
    clean = _run_final({**resumed, "run_failure": None}, prior_report, clean_files)
    assert clean["merge_ready"] is True and clean["blocking_reasons"]["status"] == "absent", clean
    # Not judged and the recompute itself can't run: never ready on an unverified seed.
    crashed = _run_final({**resumed, "run_failure": None}, prior_report, clean_files, crash=True)
    assert crashed["merge_ready"] is False and crashed["blocking_reasons"]["values"] == [erc.NOT_RECHECKED_REASON], crashed

    # targeted_fix_reasons: what a targeted fix is sent to fix. `resumed` is the state AFTER the
    # targeted-fix intake's reset (last_verification cleared); exit_stage is metrics-exit as the
    # last attempt left it -- that, not the reset state, decides whether its prose counts.
    unjudged_last = _run_final(resumed, prior_report, no_metrics_files, exit_stage={"last_verification": None})
    assert unjudged_last == [_terminal_failure_reason(rebuild_failure), erc.metrics_problems({}, "r1")[0][0]], (
        "session c2bbdca1: never chase a prior attempt's prose (no main.ts / zero screenshots)", unjudged_last
    )
    judged_last = _run_final(resumed, prior_report, no_metrics_files, exit_stage={"last_verification": {"passed": True}})
    assert prior_prose in judged_last and erc.NO_SCREENSHOTS_REASON not in judged_last, judged_last
    assert prior_report["blocking_reasons"]["values"] == [prior_prose, erc.NO_SCREENSHOTS_REASON], "stored content never mutated"
    # Recheck could not run: the placeholder is not a code problem, so there is nothing to fix.
    assert _run_final({**resumed, "run_failure": None}, prior_report, clean_files, crash=True, exit_stage={"last_verification": None}) == []

    # Every declared Check is recorded somewhere in verify_exit_readiness (text scan).
    import inspect

    verify_src = inspect.getsource(verify_exit_readiness)
    for check in VERIFY_CHECKS:
        var = next(k for k, v in globals().items() if v is check)
        assert f"log, {var}," in verify_src or f"({var}," in verify_src, f"{check.id} is declared but never recorded"

    print("exit_nodes self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.exit_nodes`
    _demo()
