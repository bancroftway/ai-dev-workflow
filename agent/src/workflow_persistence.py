"""File-based persistence for LangGraph GraphState.stages inside a repo's .ai-dev-workflow/
folder (architecture plan Section B).

Reads/writes run against a provisioned sandbox's own clone via SandboxProvider.exec_in_sandbox --
there is no local working tree on the agent's own host. Deliberately decoupled from graph.py's
StageSpec/STAGES (plain dicts and an injected render_markdown mapping instead of importing them
directly) since graph.py is this module's only caller and importing back would cycle.

Not implemented here, an explicit known gap: `attachments/` from the file layout this plan
describes -- requirements_attachments are still ephemeral, per-run-only, same as before this
module existed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from . import repo_files
from .sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

# v2: stage keys renamed to stable IDs (p0-brownfield -> brownfield-baseline, p15-exit -> exit,
# etc.). v1 state.json fails loud (HydrationError) rather than silently dropping approved stages.
SCHEMA_VERSION = 2
WORKFLOW_DIR = ".ai-dev-workflow"

_README_CONTENT = """# .ai-dev-workflow

This folder is managed by ai-dev-workflow. It persists the Requirements, Specification, and
Implementation Plan drafted and approved through the tool, so the workflow can resume across
sessions and its history is visible in this repo's own git log and pull requests.

Files here are generated -- hand-editing is unsupported and may be overwritten.
"""


class HydrationError(Exception):
    """`.ai-dev-workflow/` exists but can't be safely hydrated.

    Per the architecture plan's hydration failure contract: never silently fall back to defaults
    on a bad read (unknown schema_version, missing referenced file, malformed JSON) -- that could
    silently discard an already-approved spec/plan. Callers should let this propagate as a loud,
    specific error rather than catching and defaulting.
    """


def _stage_file(stage_key: str, kind: str) -> str:
    return f"{stage_key}.{kind}"


async def _read_file(provider: SandboxProvider, thread_id: str, relative_path: str) -> str | None:
    return await repo_files.read_repo_file(provider, thread_id, f"{WORKFLOW_DIR}/{relative_path}")


async def _write_file(provider: SandboxProvider, thread_id: str, relative_path: str, content: str) -> None:
    await repo_files.write_repo_file(provider, thread_id, f"{WORKFLOW_DIR}/{relative_path}", content)


async def hydrate_state(
    provider: SandboxProvider, thread_id: str, stage_keys: list[str]
) -> dict[str, dict[str, Any]] | None:
    """Returns the hydrated `stages` dict (GraphState["stages"] shape), or None if
    `.ai-dev-workflow/` doesn't exist yet (greenfield -- caller falls back to
    default_stage_state() per stage, same as before this module existed).
    """
    raw_state_json = await _read_file(provider, thread_id, "state.json")
    if raw_state_json is None:
        return None

    try:
        state_doc = json.loads(raw_state_json)
    except json.JSONDecodeError as exc:
        raise HydrationError(f"{WORKFLOW_DIR}/state.json is not valid JSON: {exc}") from exc

    schema_version = state_doc.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise HydrationError(
            f"{WORKFLOW_DIR}/state.json has schema_version={schema_version!r}, "
            f"this build only supports {SCHEMA_VERSION}"
        )

    stored_stages = state_doc.get("stages")
    if not isinstance(stored_stages, dict):
        raise HydrationError(f"{WORKFLOW_DIR}/state.json is missing a valid 'stages' object")

    stages: dict[str, dict[str, Any]] = {}
    for stage_key in stage_keys:
        stored = stored_stages.get(stage_key)
        if stored is None:
            continue

        draft = None
        if stored.get("has_draft"):
            draft_path = _stage_file(stage_key, "draft.json")
            draft_raw = await _read_file(provider, thread_id, draft_path)
            if draft_raw is None:
                raise HydrationError(
                    f"state.json references a draft for stage {stage_key!r} but {draft_path} is missing"
                )
            try:
                draft = json.loads(draft_raw)
            except json.JSONDecodeError as exc:
                raise HydrationError(f"{draft_path} is not valid JSON: {exc}") from exc

        approved_content = None
        if stored.get("has_approved_content"):
            approved_path = _stage_file(stage_key, "approved.json")
            approved_raw = await _read_file(provider, thread_id, approved_path)
            if approved_raw is None:
                raise HydrationError(
                    f"state.json references approved content for stage {stage_key!r} but "
                    f"{approved_path} is missing"
                )
            try:
                approved_content = json.loads(approved_raw)
            except json.JSONDecodeError as exc:
                raise HydrationError(f"{approved_path} is not valid JSON: {exc}") from exc

        stages[stage_key] = {
            "status": stored.get("status", "not_started"),
            "draft": draft,
            "clarifying_questions": stored.get("clarifying_questions", []),
            "readiness": stored.get("readiness", False),
            "cycle_count": stored.get("cycle_count", 0),
            "approved_content": approved_content,
            "ever_ready_for_review": stored.get("ever_ready_for_review", False),
            "used_ids": stored.get("used_ids", []),
            "audit_findings": stored.get("audit_findings", []),
            # Added after this module's initial version -- .get() with graph.py's
            # default_stage_state() defaults so an older state.json (predating these fields)
            # hydrates cleanly instead of producing a stage dict missing keys later code expects.
            "verify_cycle_count": stored.get("verify_cycle_count", 0),
            "last_verification": stored.get("last_verification"),
            "baseline_commit": stored.get("baseline_commit"),
        }

    logger.info("Hydrated workflow state for thread_id=%s from %s", thread_id, WORKFLOW_DIR)
    return stages


async def persist_state(
    provider: SandboxProvider,
    thread_id: str,
    *,
    stages: dict[str, dict[str, Any]],
    render_markdown: dict[str, Callable[[dict[str, Any]], str]],
) -> None:
    """Writes the given stages into `.ai-dev-workflow/`.

    Does not commit -- writing files and committing are separate steps (git_ops.py) so a commit
    failure never leaves the caller unsure whether the files themselves were actually written.

    raw-requirements.md/.draft.json/.approved.json are owned exclusively by the raw-requirements
    StageSpec entry below (via the generic per-stage render below) -- there is deliberately no
    separate hardcoded write of the raw requirements text here anymore. Before the raw-requirements
    stage existed, this function unconditionally overwrote raw-requirements.md with
    state["raw_requirements_text"] on every single persist call, including Specification/Plan
    revision cycles whose chat text had nothing to do with the requirements document -- a real
    write collision, resolved by giving raw-requirements.md exactly one writer.
    """
    stored_stages: dict[str, dict[str, Any]] = {}
    for stage_key, stage in stages.items():
        draft = stage.get("draft")
        approved_content = stage.get("approved_content")

        if draft is not None:
            await _write_file(provider, thread_id, _stage_file(stage_key, "draft.json"), json.dumps(draft, indent=2))
        if approved_content is not None:
            await _write_file(
                provider, thread_id, _stage_file(stage_key, "approved.json"), json.dumps(approved_content, indent=2)
            )

        # Human-reviewable rendering of whichever is current: approved content once approved,
        # otherwise the latest draft -- this is the file meant to be readable in a PR diff.
        content_to_render = approved_content if approved_content is not None else draft
        if content_to_render is not None:
            renderer = render_markdown.get(stage_key)
            if renderer is not None:
                await _write_file(provider, thread_id, _stage_file(stage_key, "md"), renderer(content_to_render))

        stored_stages[stage_key] = {
            "status": stage.get("status", "not_started"),
            "has_draft": draft is not None,
            "has_approved_content": approved_content is not None,
            "clarifying_questions": stage.get("clarifying_questions", []),
            "readiness": stage.get("readiness", False),
            "cycle_count": stage.get("cycle_count", 0),
            "ever_ready_for_review": stage.get("ever_ready_for_review", False),
            "used_ids": stage.get("used_ids", []),
            "audit_findings": stage.get("audit_findings", []),
            "verify_cycle_count": stage.get("verify_cycle_count", 0),
            "last_verification": stage.get("last_verification"),
            "baseline_commit": stage.get("baseline_commit"),
        }

    state_doc = {"schema_version": SCHEMA_VERSION, "stages": stored_stages}
    await _write_file(provider, thread_id, "state.json", json.dumps(state_doc, indent=2))

    if await _read_file(provider, thread_id, "README.md") is None:
        await _write_file(provider, thread_id, "README.md", _README_CONTENT)
