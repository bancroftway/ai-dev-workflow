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

# No README is written into WORKFLOW_DIR. It explained that the folder is tool-managed and
# hand-edits get overwritten, but it is one more generated file in every delivered branch for a
# reader who can see that from the numbered artifacts themselves.

class HydrationError(Exception):
    """`.ai-dev-workflow/` exists but can't be safely hydrated.

    Per the architecture plan's hydration failure contract: never silently fall back to defaults
    on a bad read (unknown schema_version, missing referenced file, malformed JSON) -- that could
    silently discard an already-approved spec/plan. Callers should let this propagate as a loud,
    specific error rather than catching and defaulting.
    """


# Pipeline order, used ONLY to prefix artifact filenames so a directory listing reads in execution
# order (`01-tech-stack.draft.json`, `02-specification.draft.json`, ...). Stage KEYS are untouched --
# nothing keys off these numbers.
#
# Duplicated here rather than imported from graph.py because graph imports THIS module; the
# self-check at the bottom asserts the two lists agree, so adding a stage to graph without adding it
# here fails loudly instead of silently dropping a stage's number.
#
# raw-requirements sorts first: it is the human's input, recorded before tech-stack drafts anything.
# brownfield-baseline sits at the end because it only runs on the brownfield entrypoint.
_STAGE_ORDER: tuple[str, ...] = (
    "raw-requirements",
    "tech-stack",
    "specification",
    "plan",
    "ac-to-tests",
    "minimal-code-to-green",
    "remediation",
    "adversarial-compliance",
    "metrics-exit",
    "brownfield-baseline",
)


def _stage_file(stage_key: str, kind: str) -> str:
    """`01-tech-stack.draft.json`. Unknown keys fall back to the unnumbered name rather than
    guessing a position -- a stage this module has never heard of should still persist."""
    try:
        index = _STAGE_ORDER.index(stage_key) + 1
    except ValueError:
        return f"{stage_key}.{kind}"
    return f"{index:02d}-{stage_key}.{kind}"


# Canonical paths for the tech-stack artifacts, DERIVED from _stage_file so they can never drift
# from the numbering. They exist as constants because five other modules read them off disk -- four
# gates and exit's verify -- and each had the unnumbered literal hardcoded. When numbering landed,
# those readers kept looking for `tech-stack.approved.json` while persistence wrote
# `02-tech-stack.approved.json`, and preflight's own writer kept the old name alive, so every branch
# carried BOTH files.
#
# `tech-stack.md` is deliberately NOT here: it holds the human's own edited markdown (written by
# preflight at gate-approval time, which is why no `02-tech-stack.md` render exists), and its name is
# a contract with the target repo -- templates/agents-md/AGENTS.md tells that repo's own agents to
# read `.ai-dev-workflow/tech-stack.md`. Renaming it would break a promise made outside this codebase.
TECH_STACK_APPROVED_PATH = f"{WORKFLOW_DIR}/{_stage_file('tech-stack', 'approved.json')}"
TECH_STACK_DRAFT_PATH = f"{WORKFLOW_DIR}/{_stage_file('tech-stack', 'draft.json')}"

# Same derived-constant reasoning. rebuild.py reads this to answer "has the implementation stage
# ever produced a draft in this workspace?" -- the precondition for its scaffold-only TDD-red gate.
# Stage bookkeeping cannot answer that on a resume (intake's hydration reset returns every
# unapproved stage to "not_started"), and this artifact can: it is written only by a real draft,
# committed to the branch, and carried on the workspace volume across container swaps.
MINIMAL_CODE_TO_GREEN_DRAFT_PATH = f"{WORKFLOW_DIR}/{_stage_file('minimal-code-to-green', 'draft.json')}"

# Same "one truth, derived from the numbering" reasoning as the tech-stack constants above --
# graph.py's plan-stage ticket-mode hydrate hook (Task 7a) reads this to detect an earlier ticket's
# approved Implementation Plan for this same project, independent of whatever this run's own
# in-memory stages["plan"] happens to hold.
PLAN_APPROVED_PATH = f"{WORKFLOW_DIR}/{_stage_file('plan', 'approved.json')}"

# Same reasoning again -- agent/src/gates/ac_coverage_gate.py's check_ac_coverage (Ruling 7) reads
# this to scope its own AC-coverage check down to THIS ticket's own ACs, since that gate has only
# thread_id/provider (no GraphState) and so cannot read state["stages"]["specification"]
# ["approved_content"] the way spec_ledger.hydrate_ac_to_tests_ticket_mode_context does for the
# identical "this ticket's own AC ids" question.
SPECIFICATION_APPROVED_PATH = f"{WORKFLOW_DIR}/{_stage_file('specification', 'approved.json')}"

# Same reasoning again -- graph.py's hydrate_remediation_ticket_mode_context (Task 7b) reads this
# to detect an earlier ticket's own approved remediation report for this same project, so a later
# ticket's draft can carry that ticket's accepted `known_gaps` reasoning forward instead of
# re-investigating an unrelated, still-open finding from scratch on every single ticket.
REMEDIATION_APPROVED_PATH = f"{WORKFLOW_DIR}/{_stage_file('remediation', 'approved.json')}"

# Same reasoning again -- exit_finalize_node hashes this into manifest.json's
# requirements_content_hash. It read the UNNUMBERED literal for as long as the numbering existed,
# so the hash was silently None on every run (the file it looked for was never written).
RAW_REQUIREMENTS_APPROVED_PATH = f"{WORKFLOW_DIR}/{_stage_file('raw-requirements', 'approved.json')}"

# The FULL exit report (health table, findings dispositions, scanner tools). Written ONLY by
# exit_nodes.exit_finalize_node -- metrics-exit's StageSpec sets render_markdown=None so the
# generic per-stage .md render above never touches it (it would revert the full report to the
# 4-section merge-readiness stub on the next run's first persist).
METRICS_EXIT_MD_PATH = f"{WORKFLOW_DIR}/{_stage_file('metrics-exit', 'md')}"


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

        # A stage artifact that is not a JSON object is from a superseded schema and cannot be fed
        # to this build's renderers or gates (which index it by key). Observed: remediation was
        # persisted as a bare summary STRING, because its content_field named one str field of the
        # response. Dropping it re-runs the stage under the current schema, which is the only
        # outcome that yields a usable artifact -- keeping it crashes persistence on every write.
        superseded = [
            name
            for name, value in (("draft", draft), ("approved", approved_content))
            if value is not None and not isinstance(value, dict)
        ]
        if superseded:
            logger.warning(
                "stage %s: discarding non-object %s artifact(s) from a superseded schema -- the "
                "stage will re-run",
                stage_key,
                "/".join(superseded),
            )
            draft = draft if isinstance(draft, dict) else None
            approved_content = approved_content if isinstance(approved_content, dict) else None

        stages[stage_key] = {
            "status": "not_started" if superseded else stored.get("status", "not_started"),
            "draft": draft,
            "clarifying_questions": stored.get("clarifying_questions", []),
            "readiness": stored.get("readiness", False),
            "cycle_count": stored.get("cycle_count", 0),
            "approved_content": approved_content,
            "ever_ready_for_review": stored.get("ever_ready_for_review", False),
            "used_ids": stored.get("used_ids", []),
            "audit_findings": stored.get("audit_findings", []),
            "skills": stored.get("skills"),
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
    render_markdown: dict[str, Callable[[dict[str, Any]], str] | None],
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
            # Which skills this stage's sessions actually invoked, from their own skill.invoked
            # events -- see gates/skill_gate.skills_record. Lives in state.json rather than inside
            # the draft/approved JSON because those hold MODEL output, and pipeline metadata mixed
            # into content that gets re-validated on hydration is asking for trouble.
            "skills": stage.get("skills"),
            "verify_cycle_count": stage.get("verify_cycle_count", 0),
            "last_verification": stage.get("last_verification"),
            "baseline_commit": stage.get("baseline_commit"),
        }

    state_doc = {"schema_version": SCHEMA_VERSION, "stages": stored_stages}
    await _write_file(provider, thread_id, "state.json", json.dumps(state_doc, indent=2))


def _demo() -> None:
    """Self-check for the pure half: `cd agent && uv run python -m src.workflow_persistence`."""
    # Numbered, zero-padded, execution-ordered.
    assert _stage_file("tech-stack", "draft.json") == "02-tech-stack.draft.json"
    assert _stage_file("raw-requirements", "md") == "01-raw-requirements.md"
    assert _stage_file("metrics-exit", "approved.json") == "09-metrics-exit.approved.json"
    # A key this module does not know still persists, unnumbered, rather than crashing or guessing.
    assert _stage_file("some-future-stage", "draft.json") == "some-future-stage.draft.json"

    # The tech-stack constants must track the numbering, not a stale literal -- five modules read
    # them off disk, and when numbering landed those readers were still looking for the unnumbered
    # name while persistence wrote the numbered one, so branches carried BOTH files.
    assert TECH_STACK_APPROVED_PATH == f"{WORKFLOW_DIR}/02-tech-stack.approved.json"
    assert TECH_STACK_DRAFT_PATH == f"{WORKFLOW_DIR}/02-tech-stack.draft.json"
    assert PLAN_APPROVED_PATH == f"{WORKFLOW_DIR}/04-plan.approved.json"
    assert SPECIFICATION_APPROVED_PATH == f"{WORKFLOW_DIR}/03-specification.approved.json"
    assert REMEDIATION_APPROVED_PATH == f"{WORKFLOW_DIR}/07-remediation.approved.json"
    assert RAW_REQUIREMENTS_APPROVED_PATH == f"{WORKFLOW_DIR}/01-raw-requirements.approved.json"

    # Padding must keep lexical order == execution order; a bare str(n) breaks at 10 stages.
    names = [_stage_file(k, "md") for k in _STAGE_ORDER]
    assert names == sorted(names), names

    # Drift guard: graph owns the stage list, this module owns their numbering. Imported lazily --
    # graph imports this module, so a top-level import here would be circular.
    from .graph import _STAGE_KEYS

    assert set(_STAGE_ORDER) == set(_STAGE_KEYS), (
        f"stage list drift: only in _STAGE_ORDER={set(_STAGE_ORDER) - set(_STAGE_KEYS)}, "
        f"only in graph._STAGE_KEYS={set(_STAGE_KEYS) - set(_STAGE_ORDER)}"
    )

    print("workflow_persistence self-check: all assertions passed")


if __name__ == "__main__":
    _demo()
