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
    "raw-requirements",
    "specification",
    "plan",
    "ac-to-tests",
    "minimal-code-to-green",
    "p11a-adversarial-audit",
    "p11b-dedup",
    "p11d-license-audit",
    "p15-exit",
    "p0-brownfield",
    "p8-quality",
    "p10-security",
    "p11c-upgrade",
    "p13-flake-triage",
    "p14-metrics",
]
Role = Literal["draft", "audit"]


@lru_cache(maxsize=None)
def _load_config() -> dict[str, dict[str, str | None]]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_model_name(stage: Stage, role: Role) -> str | None:
    stage_config = _load_config().get(stage, {})
    draft_model = stage_config.get("draft_model")
    if role == "draft":
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
