"""Runtime configuration (SPECIFICATION.md US-10: configurable safety cap)."""

from __future__ import annotations

import os

SPEC_MAX_CLARIFICATION_CYCLES = int(os.environ.get("SPEC_MAX_CLARIFICATION_CYCLES", "3"))
PLAN_MAX_CLARIFICATION_CYCLES = int(os.environ.get("PLAN_MAX_CLARIFICATION_CYCLES", "3"))

COPILOT_MODEL_NAME = os.environ.get("COPILOT_MODEL_NAME") or None
