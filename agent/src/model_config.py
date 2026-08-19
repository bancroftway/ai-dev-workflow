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
    "adversarial-audit",
    "dedup-simplify",
    "license-audit",
    "exit",
    "brownfield-baseline",
    "quality-remediation",
    "security-remediation",
    "finding-cluster-upgrade",
    "test-hardening-flake-triage",
    "metrics-report",
    "rebuild",
    "e2e",
    "session-title",
]
Role = Literal["draft", "audit", "extract"]


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
