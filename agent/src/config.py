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

# Skills that are loaded but must never be offered to a pipeline session. Both are written as
# standing MANDATES rather than opt-in capabilities -- using-superpowers' own description is
# "Use when starting any conversation ... requiring skill invocation before ANY response", and
# brainstorming's is "You MUST use this before any creative work". Confirmed live: with these
# reachable, ac-to-tests-draft spent its turn calling skills 10x and its own edit tools 0x, and
# escalated with zero test files written.
#
# The rest of the superpowers pack is the opposite -- narrow, opt-in, and already named by this
# repo's own prompts (test-driven-development in ac_to_tests_draft.md, systematic-debugging in
# rebuild_build_fix.md/e2e_fix.md, subagent-driven-development + executing-plans in
# minimal_code_to_green_draft.md, verification-before-completion + receiving-code-review in the
# audit prompts). Excluding the whole plugin turned every one of those into a dangling reference
# to a skill the session could not load; disabling just the two mandates keeps the referenced
# skills working.
COPILOT_DISABLED_SKILLS = ["using-superpowers", "brainstorming"]

# specification is the ONE stage where brainstorming belongs -- its whole job is exploring intent
# and requirements before anything is built, which is exactly what that skill is for. Everywhere
# else it fires as a blanket "you MUST brainstorm before any creative work" mandate on stages that
# are mechanical (write these tests, run this build) and burns the turn. using-superpowers stays
# disabled everywhere: it is a meta-router that mandates invoking A skill before ANY response,
# including before clarifying questions, and no stage wants that.
COPILOT_DISABLED_SKILLS_SPECIFICATION = ["using-superpowers"]

# Skills each stage is REQUIRED to invoke, enforced deterministically rather than trusted: the
# stage's prompt names them, and gates/skill_gate.py verifies against the Copilot session's own
# `skill.invoked` events (the model's self-report in StageReport.skills_invoked is telemetry, not
# evidence -- a model that skipped a skill will happily claim it used one).
REQUIRED_SKILLS_BY_STAGE: dict[str, list[str]] = {
    "specification": ["brainstorming"],
    "plan": ["writing-plans"],
    "ac-to-tests": ["test-driven-development"],
    "minimal-code-to-green": ["executing-plans", "requesting-code-review", "verification-before-completion"],
    "adversarial-compliance": ["receiving-code-review", "verification-before-completion"],
    "metrics-exit": ["finishing-a-development-branch"],
    # dispatching-parallel-agents is deliberately NOT required: it applies only when the plan has
    # genuinely independent steps, so mandating it would force a nonsense invocation on a linear
    # plan. systematic-debugging likewise -- the fix nodes it belongs to only run on failure.
}

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
