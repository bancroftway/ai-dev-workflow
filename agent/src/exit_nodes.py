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
import os
import shlex
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig

from . import approvals, git_ops, preflight_nodes, repo_files, repo_scan, session_index, spec_ledger
from .markdown_render import render_exit_markdown
from .preflight_nodes import MANIFEST_PATH
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

logger = logging.getLogger(__name__)

CHANGELOG_PATH = "CHANGELOG.md"
HISTORY_DIR = ".ai-dev-workflow/history"

# N most recent runs (by sessions.json order) whose history/ artifacts survive exit finalize.
# Screenshots are committed binaries -- unbounded per-run growth ships to the user's own remote.
_DEFAULT_HISTORY_RETAIN = 10


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


def _history_retain() -> int:
    """AIDW_HISTORY_RETAIN, same tolerant-parse pattern as session_index._file_cap."""
    try:
        configured = int(os.environ.get("AIDW_HISTORY_RETAIN") or _DEFAULT_HISTORY_RETAIN)
    except ValueError:
        configured = _DEFAULT_HISTORY_RETAIN
    return max(configured, 1)


def _stale_history_files(filenames: list[str], keep_run_ids: set[str]) -> list[str]:
    """Pure grouping half of retention. run_id is an 8-hex-char token (graph.py intake_node), never
    containing "-", so splitting each filename on its FIRST "-" reliably recovers the run_id prefix
    regardless of which artifact kind follows it (-report.json, -exit.md, -metrics.json,
    -ledger-snapshot.json, -screens). Lexical filename sort would not be chronological (a hex token
    isn't a timestamp) -- that's why this groups by run_id and checks membership instead."""
    return [name for name in filenames if name.split("-", 1)[0] not in keep_run_ids]


def _prune_keep_ids(sessions: list[dict[str, Any]], run_id: str) -> set[str] | None:
    """Pure decision half of retention: which run_ids survive pruning, or None to skip pruning
    entirely this run.

    FAIL-CLOSED on an empty `sessions`: session_index._read is fail-OPEN by design (a corrupt file
    or a transient read glitch returns [] so every existing caller can treat that as "no sessions
    yet" and move on harmlessly). This is the first caller that uses the result to justify
    DELETION -- collapsing keep_ids down to just the current run on a bad read would rm -rf every
    OTHER run's history artifacts (reports, screenshots, snapshots) right before the commit. An
    empty `sessions` here returns None instead, so the caller skips pruning rather than trusting it.
    """
    if not sessions:
        return None
    keep_ids = {s.get("run_id") for s in sessions[-_history_retain():] if s.get("run_id")}
    keep_ids.add(run_id)
    return keep_ids


async def _prune_history(provider: Any, thread_id: str, run_id: str) -> None:
    """Deletes history/ artifacts belonging to runs older than the last N (AIDW_HISTORY_RETAIN,
    default 10), keeping every run_id mentioned in the last N sessions.json entries (chronological,
    see session_index.py) plus always the current run -- run_id may not appear there yet if the
    exit stage was never approved (end_session only fires when merge_readiness is truthy, above).
    Skips pruning entirely (loudly, if history/ is non-empty) when sessions.json read back empty --
    see _prune_keep_ids for why.
    """
    sessions = await session_index._read(provider, thread_id)  # noqa: SLF001 -- same package, read-only reuse

    listing = await provider.exec_in_sandbox(thread_id, f"ls {shlex.quote(HISTORY_DIR)} 2>/dev/null")
    filenames = [n.strip() for n in (listing.stdout or "").splitlines() if n.strip()]

    keep_ids = _prune_keep_ids(sessions, run_id)
    if keep_ids is None:
        if filenames:
            logger.warning(
                "exit_nodes: history/ has %d file(s) but sessions.json read back empty for "
                "thread_id=%s -- skipping retention prune this run instead of deleting every "
                "other run's artifacts on what may be a transient read glitch.",
                len(filenames), thread_id,
            )
        return

    stale = _stale_history_files(filenames, keep_ids)
    if not stale:
        return
    quoted = " ".join(shlex.quote(f"{HISTORY_DIR}/{name}") for name in stale)
    await provider.exec_in_sandbox(thread_id, f"rm -rf -- {quoted}")


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


def _render_history_sections(
    *,
    files_changed_stat: str,
    commits_log: str,
    metrics_summary: dict[str, Any],
    delta_summary: dict[str, Any] | None,
    screenshots: list[str],
    run_id: str,
    e2e: dict[str, Any] | None = None,
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

    # Unconditional section -- the exit report has a fixed skeleton, and "no screenshots" must be
    # a stated fact with a reason, never a silently missing heading.
    lines += ["## E2E Screenshots", ""]
    if screenshots:
        for path in screenshots:
            name = path.rsplit("/", 1)[-1]
            lines.append(f"![{name}](./{run_id}-screens/{name})")
    else:
        reason = e2e.get("skipped_reason") or ""
        lines.append(f"(none captured -- e2e {e2e_status}{': ' + reason if reason else ''})")
    lines.append("")

    return "\n".join(lines)


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
    thread_id: str, content_dict: dict[str, Any], run_id: str, baseline_commit: str | None, provider: Any
) -> Any:
    """EXIT_SPEC's deterministic_verify: completes the manifest (greenfield re-record + commands --
    only exit has the complete picture, code exists and coverage-commands.json is final), then
    forces merge_ready=False on the merge-readiness draft for any deterministic blocker: the
    metrics regression gate's recorded reasons, a UI app with zero e2e screenshots, or a manifest
    still missing apps/test_command/coverage_commands. Always returns passed=True with the draft
    mutated in place -- an LLM redraft can't fix a code regression or a missing screenshot; the
    downgrade IS the outcome (the no-sandbox cannot_verify path is handled by make_verify_node)."""
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
    tech_stack = _parse(await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/tech-stack.approved.json"))

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
    else:
        problems.append("metrics were not recorded for this run -- the regression gate never passed")

    if problems:
        content_dict["merge_ready"] = False
        existing = list(content_dict.get("blocking_reasons") or [])
        content_dict["blocking_reasons"] = existing + [p for p in problems if p not in existing]
        feedback = f"merge_ready forced False: {len(problems)} deterministic blocker(s)"
    else:
        feedback = "deterministic exit checks passed (manifest complete, screenshots present for UI, metrics gate clean)"
    return VerificationResult(
        passed=True,
        feedback=feedback,
        report={"blockers": problems, "ui_app": is_ui, "screenshot_count": len(screenshots), "manifest_completed": sorted(updates)},
    )


async def exit_finalize_node(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    if sandbox_registry.get(thread_id) is None:
        return {}

    provider = get_sandbox_provider()
    run_id = state.get("run_id", "unknown")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    spec_approval = await approvals.latest_approval(provider, thread_id, "specification")
    plan_approval = await approvals.latest_approval(provider, thread_id, "plan")
    raw_requirements = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/raw-requirements.approved.json")
    raw_metrics = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/metrics-latest.json")
    metrics_summary = json.loads(raw_metrics) if raw_metrics else {}
    if metrics_summary.get("run_id") != run_id:
        # Stale file from a previous run (metrics_compute short-circuited this run) -- rendering
        # it as this run's numbers was the "traceability from a stale manifest" bug. Say "not
        # recorded" instead, and persist nothing stale.
        metrics_summary = {}
    exit_stage = (state.get("stages") or {}).get("exit", {})
    merge_readiness = exit_stage.get("approved_content")

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

    if merge_readiness or state.get("run_failure"):
        # A metrics-regression run reaches here too (the gate routes INTO exit, never END) -- close
        # its session row as failed so it never lingers as "running" in sessions.json.
        await session_index.end_session(
            provider,
            thread_id,
            run_id=run_id,
            status="failed" if state.get("run_failure") else "completed",
            failure=state.get("run_failure"),
            exit_summary={
                "merge_ready": (merge_readiness or {}).get("merge_ready", False),
                "pr_title": (merge_readiness or {}).get("pr_title", ""),
            } if merge_readiness else None,
        )

    # Per-run exit report artifacts (durable even once sessions.json ages the row out): the raw
    # diff/log this run actually produced, plus the same metrics/delta numbers the frontend Report
    # tab shows live, frozen at exit time so a past session's report page can render identically.
    files_changed_stat, commits_log = await _files_changed(provider, thread_id, state.get("run_baseline_commit"))
    screenshots = await _list_screenshots(provider, thread_id, run_id)
    delta_summary = repo_scan.delta_summary(metrics_summary.get("repo_scan_delta"))

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
    }
    await repo_files.write_repo_file(provider, thread_id, report_path, json.dumps(report_payload, indent=2, default=str) + "\n")

    exit_markdown = render_exit_markdown(merge_readiness or {}) + "\n" + _render_history_sections(
        files_changed_stat=files_changed_stat,
        commits_log=commits_log,
        metrics_summary=metrics_summary,
        delta_summary=delta_summary,
        screenshots=screenshots,
        run_id=run_id,
        e2e=state.get("e2e"),
    )
    await repo_files.write_repo_file(provider, thread_id, exit_md_path, exit_markdown)

    # Retention: prune older runs' history/ artifacts before the commit below picks up whatever's
    # left (new writes above + any deletions here) in one `git add` on the whole directory.
    await _prune_history(provider, thread_id, run_id)

    await git_ops.commit_paths(
        provider,
        thread_id,
        [MANIFEST_PATH, HISTORY_DIR, CHANGELOG_PATH, session_index.SESSIONS_PATH],
        "ai-dev-workflow: exit finalize (manifest, changelog, exit report)",
    )
    return {}


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

    # _history_retain: env override, tolerant fallback, floor of 1.
    os.environ.pop("AIDW_HISTORY_RETAIN", None)
    assert _history_retain() == _DEFAULT_HISTORY_RETAIN
    os.environ["AIDW_HISTORY_RETAIN"] = "3"
    assert _history_retain() == 3
    os.environ["AIDW_HISTORY_RETAIN"] = "not-a-number"
    assert _history_retain() == _DEFAULT_HISTORY_RETAIN
    os.environ["AIDW_HISTORY_RETAIN"] = "0"
    assert _history_retain() == 1
    os.environ.pop("AIDW_HISTORY_RETAIN", None)

    # _stale_history_files: groups by run_id (first "-") prefix, keeps whole groups, including a
    # -screens directory entry alongside a run's other artifact files.
    files = [
        "aaaaaaaa-report.json", "aaaaaaaa-exit.md", "aaaaaaaa-screens",
        "bbbbbbbb-report.json", "bbbbbbbb-metrics.json",
    ]
    assert _stale_history_files(files, {"aaaaaaaa"}) == ["bbbbbbbb-report.json", "bbbbbbbb-metrics.json"]
    assert _stale_history_files(files, {"aaaaaaaa", "bbbbbbbb"}) == []
    assert _stale_history_files(files, set()) == files

    # _prune_keep_ids: empty sessions -> None (fail-closed, caller must skip pruning entirely --
    # NOT "keep only the current run", which would rm -rf every other run's artifacts on a
    # transient session_index._read glitch). Non-empty sessions -> the last N run_ids + current.
    assert _prune_keep_ids([], "current") is None
    sessions = [{"run_id": f"r{i}"} for i in range(5)]
    os.environ["AIDW_HISTORY_RETAIN"] = "2"
    assert _prune_keep_ids(sessions, "current") == {"r3", "r4", "current"}
    os.environ.pop("AIDW_HISTORY_RETAIN", None)

    # _render_history_sections: "not recorded"/"no baseline" placeholders when data is absent,
    # real content when present, and a FIXED skeleton -- the screenshots section always renders,
    # stating why it's empty (e2e status + skip reason) rather than silently missing.
    empty = _render_history_sections(
        files_changed_stat="", commits_log="", metrics_summary={}, delta_summary=None, screenshots=[], run_id="r1",
        e2e={"status": "skipped", "skipped_reason": "no UI framework"},
    )
    assert "not recorded for this run" in empty.lower()
    assert "no baseline recorded" in empty.lower()
    assert "## E2E Screenshots" in empty
    assert "(none captured -- e2e skipped: no UI framework)" in empty
    assert "- **E2E**: skipped -- no UI framework" in empty

    filled = _render_history_sections(
        files_changed_stat="1 file changed",
        commits_log="abc123 do the thing",
        metrics_summary={"coverage": {"line_rate": 80.0, "branch_rate": 70.0}, "traceability_summary": {"total": 2, "covered": 1, "tests_only": 1, "untested": 0}, "token_usage_summary": {"total_input_tokens": 100, "total_output_tokens": 50, "total_cost": 0.01}},
        delta_summary={"fixed_count": 1, "introduced_count": 0, "severity_changed": 0, "metrics": {"coverage_line_rate": {"from": 70, "to": 80, "delta": 10, "direction": "improved"}}},
        screenshots=[".ai-dev-workflow/history/r1-screens/1.png"],
        run_id="r1",
        e2e={"status": "passed", "total": 3, "passed": 3, "failed_tests": []},
    )
    assert "1 file changed" in filled
    assert "80.0%" in filled
    assert "coverage_line_rate" in filled
    assert "- **E2E**: passed" in filled
    assert "## E2E Screenshots" in filled and "./r1-screens/1.png" in filled
    assert "(none captured" not in filled

    print("exit_nodes self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.exit_nodes`
    _demo()
