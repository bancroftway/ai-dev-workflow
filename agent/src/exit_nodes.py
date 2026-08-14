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
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig

from . import approvals, git_ops, preflight_nodes, repo_files, spec_ledger
from .preflight_nodes import MANIFEST_PATH
from .sandbox import registry as sandbox_registry
from .sandbox.factory import get_sandbox_provider

CHANGELOG_PATH = "CHANGELOG.md"


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
    snapshot_path = f".ai-dev-workflow/history/{run_id}-ledger-snapshot.json"
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
    await git_ops.commit_paths(
        provider, thread_id, [MANIFEST_PATH, snapshot_path, CHANGELOG_PATH], "ai-dev-workflow: exit finalize (manifest, changelog)"
    )
    return {}
