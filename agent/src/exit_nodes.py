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

from . import approvals, copilot_chat_model, git_ops, preflight_nodes, repo_files, repo_scan, session_store, spec_ledger, workflow_persistence
from .markdown_render import render_exit_markdown
from .preflight_nodes import MANIFEST_PATH
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

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

    # Unconditional sections -- the exit report has a fixed skeleton, and "no screenshots" / "not
    # evaluated" must be stated facts with reasons, never silently missing headings.
    lines += _render_eval_section(metrics_summary)
    lines += _render_supply_chain_section(metrics_summary)
    lines += _render_skills_section(stages)

    lines += ["## Screens", ""]
    if screenshots:
        lines += ["| Screen | Route | Screenshot |", "|---|---|---|"]
        for path in screenshots:
            name = path.rsplit("/", 1)[-1]
            screen, route = _screen_label(name)
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
        stages=state.get("stages"),
    )
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
    )
    await repo_files.write_repo_file(provider, thread_id, EXIT_REPORT_PATH, latest_markdown)

    await git_ops.commit_paths(
        provider,
        thread_id,
        [MANIFEST_PATH, HISTORY_DIR, CHANGELOG_PATH, EXIT_REPORT_PATH],
        "ai-dev-workflow: exit finalize (manifest, changelog, exit report)",
    )

    # Graceful end-of-run release of this thread's ~20 Copilot sessions. metrics-exit is genuinely
    # the last stage -- every other terminal path (metrics regression, test-hardening, e2e escalate)
    # routes INTO metrics-exit_draft rather than END, and all four POST_STAGE_REBUILD placements sit
    # before it -- so nothing downstream needs a session. run_headless.py already did this at
    # process exit; the server path never did, which left every completed run's sessions riding
    # until the sandbox idle-reaper eventually took the container down.
    # Deliberately NOT done on the needs_clarification -> END path: there the user is about to
    # answer the model's own question, and that stage's conversation continuity is wanted.
    await copilot_chat_model.close_thread_session(thread_id)


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

    print("exit_nodes self-check: ok")


if __name__ == "__main__":  # pragma: no cover -- `cd agent && uv run python -m src.exit_nodes`
    _demo()
