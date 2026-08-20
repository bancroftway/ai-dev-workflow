"""Per-stage, per-role GitHub Copilot model configuration (SPECIFICATION.md Section 3.4).

Loaded once from agent/config/models.yaml -- editable without touching code, same principle as
agent/src/prompts/*.md. Replaces the old single global COPILOT_MODEL_NAME env var.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "models.yaml"

Stage = Literal[
    "tech-stack",
    "specification",
    "plan",
    "ac-to-tests",
    "minimal-code-to-green",
    "remediation",
    "adversarial-compliance",
    "metrics-exit",
    "brownfield-baseline",
    "rebuild",
    "e2e",
    "e2e-run",
    "coverage-run",
    "ac-test-run",
    "stack-run",
    "test-hardening-run",
    "test-hardening-flake-triage",
    "metrics-report",
    "session-title",
]
# "fix" is a WRITE-capable pass that closes what a stage's deterministic verify reported (see
# graph.make_verify_fix_node). Declared so a stage can give it a different model from its draft.
Role = Literal["draft", "audit", "extract", "fix"]


@lru_cache(maxsize=None)
def _load_config() -> dict[str, dict[str, str | None]]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_model_name(stage: Stage, role: Role) -> str | None:
    stage_config = _load_config().get(stage, {})
    draft_model = stage_config.get("draft_model")
    if role in ("draft", "extract"):
        # "extract" deliberately shares draft_model rather than needing its own models.yaml entry
        # -- a one-shot structured-extraction pass is not a config-worthy decision distinct from
        # drafting, unlike audit (a genuinely separate model choice, hence its own warning below).
        return draft_model

    if role == "fix":
        # Explicit branch, because without it "fix" fell through to the audit lookup below: the
        # `fix_model` key was silently ignored (dead config) and every fix pass logged "No
        # audit_model configured", which is a misleading thing to print about a write pass.
        # Falls back to draft_model rather than audit_model -- fixing is a code-writing job, and
        # the audit tier is chosen to critique, sometimes on a cheaper model.
        return stage_config.get("fix_model") or draft_model

    audit_model = stage_config.get("audit_model")
    if audit_model is None:
        logger.warning(
            "No audit_model configured for stage %r in models.yaml; falling back to "
            "draft_model %r. The audit pass will run, but against the same model as the "
            "draft it's meant to critique.",
            stage,
            draft_model,
        )
        return draft_model
    return audit_model
