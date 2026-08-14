"""P1 plain (non-LLM) helper: the raw-requirements stage's idempotency short-circuit.

Kept as a separate module from preflight_nodes.py (brownfield-baseline's own plain-node module) since this one
concern -- "does this run's fresh input warrant redrafting" -- is P1-specific and has nothing to
do with brownfield-baseline's scaffold/tech-stack helpers; matches the existing one-concern-per-module convention.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from . import git_ops, repo_files
from .sandbox.provider import SandboxProvider

if TYPE_CHECKING:
    from .graph import GraphState

logger = logging.getLogger(__name__)


_SEED_SIDECAR_PATH = ".ai-dev-workflow/raw-requirements.seed.txt"


async def persist_raw_requirements_seed(
    thread_id: str, _content_dict: dict[str, Any], state: "GraphState", provider: SandboxProvider
) -> None:
    """StageSpec.post_audit_hook for the raw-requirements stage: records the seed text that
    produced this run's draft, in a sidecar file separate from the drafted document itself.

    Load-bearing, not cosmetic: the drafted RawRequirementsDocument is the LLM's reorganized,
    expanded prose -- comparing it directly against a future run's raw human seed text would
    (and, caught by real end-to-end testing, did) always report "changed" even when the human
    submitted the exact same seed twice, since a one-paragraph note and a multi-section Markdown
    document are never textually equal. This sidecar is hydrate_raw_requirements_from_repo_file's
    actual comparison baseline.
    """
    seed_text = state.get("raw_requirements_text", "")
    await repo_files.write_repo_file(provider, thread_id, _SEED_SIDECAR_PATH, seed_text)
    await git_ops.commit_paths(provider, thread_id, [_SEED_SIDECAR_PATH], "ai-dev-workflow: record requirements seed")


async def hydrate_raw_requirements_from_repo_file(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.hydrate_from_repo_file for the raw-requirements stage.

    Load-bearing correction from the plan (not optional): skip-and-hydrate applies only when
    raw-requirements.approved.json already exists AND this run's freshly-submitted seed text
    (state["raw_requirements_text"], pulled by intake_node from the latest HumanMessage) is
    either empty or identical to the seed text that produced the currently-approved draft
    (persist_raw_requirements_seed's sidecar, not the drafted document itself -- see its own
    docstring for why those two are not comparable). Any resubmission with genuinely new text
    bypasses the shortcut and redrafts -- otherwise a human editing the Requirements tab and
    resubmitting would have their edit silently ignored on every run after the first.
    """
    raw = await repo_files.read_repo_file(provider, thread_id, ".ai-dev-workflow/raw-requirements.approved.json")
    if raw is None:
        return None
    try:
        approved = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "raw-requirements.approved.json exists but isn't valid JSON for thread_id=%s -- "
            "treating as needing a fresh run rather than failing hard.",
            thread_id,
        )
        return None

    fresh_text = state.get("raw_requirements_text", "")
    seed_text = await repo_files.read_repo_file(provider, thread_id, _SEED_SIDECAR_PATH) or ""
    if fresh_text and fresh_text != seed_text:
        return None  # genuinely new/edited input this run -- redraft, don't hydrate
    return approved
