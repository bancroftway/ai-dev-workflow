"""Runtime configuration (SPECIFICATION.md US-10: configurable safety cap)."""

from __future__ import annotations

import os

SPEC_MAX_CLARIFICATION_CYCLES = int(os.environ.get("SPEC_MAX_CLARIFICATION_CYCLES", "3"))
PLAN_MAX_CLARIFICATION_CYCLES = int(os.environ.get("PLAN_MAX_CLARIFICATION_CYCLES", "3"))
AC_TO_TESTS_MAX_CLARIFICATION_CYCLES = int(os.environ.get("AC_TO_TESTS_MAX_CLARIFICATION_CYCLES", "3"))
MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES = int(
    os.environ.get("MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES", "3")
)
ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES = int(os.environ.get("ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES", "2"))
EXIT_MAX_CLARIFICATION_CYCLES = int(os.environ.get("EXIT_MAX_CLARIFICATION_CYCLES", "2"))
# Small default: tech-stack detection is autonomous codebase study, not human-clarification-driven,
# so this safety cap should rarely if ever trigger.
TECH_STACK_MAX_CLARIFICATION_CYCLES = int(os.environ.get("TECH_STACK_MAX_CLARIFICATION_CYCLES", "2"))
# Deliberately 1, and load-bearing: with a not-ready draft, make_route_after_draft sends
# cycle_count >= max_cycles to auto_approve rather than to needs_clarification -> END. App
# discovery gates the entire pipeline, so a model that withholds readiness must not be able to
# halt the run silently -- an empty report reaches the decide node, which fails closed with a
# reason that names the real cause.
APP_DISCOVERY_MAX_CLARIFICATION_CYCLES = int(os.environ.get("APP_DISCOVERY_MAX_CLARIFICATION_CYCLES", "1"))

# e2e's own bespoke-cluster caps (agent/src/e2e_nodes.py): fix-cycle cap (same shape as
# rebuild.py's max_fix_cycles), app-boot readiness timeout, and the whole playwright suite's own
# timeout (wrapped in `timeout <n>` so a hung suite can't wedge the sandbox forever).
E2E_MAX_FIX_CYCLES = int(os.environ.get("E2E_MAX_FIX_CYCLES", "2"))
E2E_APP_READY_TIMEOUT_SECONDS = int(os.environ.get("E2E_APP_READY_TIMEOUT_SECONDS", "120"))
E2E_SUITE_TIMEOUT_SECONDS = int(os.environ.get("E2E_SUITE_TIMEOUT_SECONDS", "1200"))

# In-container path the sandbox image bakes the Agent Plugin content to (agent/sandbox-image/
# Dockerfile's COPY plugins/ -> this path). Overridable for local spikes without a code change.
COPILOT_PLUGIN_ROOT_IN_CONTAINER = os.environ.get(
    "COPILOT_PLUGIN_ROOT_IN_CONTAINER", "/opt/ai-dev-workflow-plugins"
)
COPILOT_PLUGIN_DIRECTORIES = [
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/ai-dev-workflow",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/obra-superpowers/superpowers",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/dietrichgebert-ponytail/ponytail",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/juliusbrussee-caveman/caveman",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/github-awesome-copilot/security-review",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/pbakaus-impeccable/impeccable",
]

# Read-only tool allowlist (Phase A0 spike finding: excluded_tools blocklisting write-capable
# tools is incomplete -- the model can reach create/bash/edit/apply_patch interchangeably, so
# read-only stages must allowlist via available_tools instead). All entries are source-qualified
# ("builtin:<name>") per copilot._mode.ToolSet -- bare names are rejected/silently ignored.
READ_ONLY_AVAILABLE_TOOLS = [
    "builtin:view",
    "builtin:grep",
    "builtin:glob",
    "builtin:task_complete",
    "builtin:ask_user",
    "builtin:skill",
]
