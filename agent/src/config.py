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
# 8: the e2e loop's job is to FIX the app, not to exit early (user directive 2026-08-21) -- a
# failing acceptance journey is a code bug, and escalating hands a human a broken app. Observed
# live: 0/6 -> 4/6 in two laps (run 13), so real convergence spans many laps. The cap exists only
# as a runaway backstop, not as an expected exit.
E2E_MAX_FIX_CYCLES = int(os.environ.get("E2E_MAX_FIX_CYCLES", "8"))
# Same philosophy for stable unit/integration-test regressions: repair in-pipeline, cap only as a
# runaway backstop (see test_hardening_nodes.test_hardening_fix_node).
TEST_HARDENING_MAX_FIX_CYCLES = int(os.environ.get("TEST_HARDENING_MAX_FIX_CYCLES", "4"))
E2E_APP_READY_TIMEOUT_SECONDS = int(os.environ.get("E2E_APP_READY_TIMEOUT_SECONDS", "120"))
E2E_SUITE_TIMEOUT_SECONDS = int(os.environ.get("E2E_SUITE_TIMEOUT_SECONDS", "1200"))
# Lighthouse (performance + accessibility) runs inside e2e_run_node's live-app window -- the ONE
# place a served app exists (deliberately NOT a repo_scan tool: repo_scan's contract is offline,
# no running app). Worst-of-routes scores (0-100) below either floor count as an e2e failure and
# feed the same e2e_fix loop/cap above with the failing audit titles. 0 disables that gate (scores
# still measured and reported). Defaults: a11y gated at 90 (axe-backed, deterministic, and its
# failing audits are concrete code fixes an LLM lap can actually make); perf REPORT-ONLY by
# default -- dev-server numbers on the headless shell are timing-noisy, and a score hovering near
# a floor flip-flops across fix laps, burning up to E2E_MAX_FIX_CYCLES paid model turns on a
# number a code change can't reliably move (2026-08-24 audit). Set a floor explicitly to gate it.
LIGHTHOUSE_PERF_MIN = int(os.environ.get("LIGHTHOUSE_PERF_MIN", "0"))
LIGHTHOUSE_A11Y_MIN = int(os.environ.get("LIGHTHOUSE_A11Y_MIN", "90"))
# Audit ids that block the e2e gate on their own, whatever the aggregate score: an accessibility
# score of 93 sailed past the floor while `color-contrast` scored 0 on a primary button (run
# d16959d3) -- a WCAG AA failure on a delivered UI is a defect, not a rounding error. Comma-separated
# Lighthouse audit ids; empty disables. Each is a concrete, selector-named fix the e2e_fix lap can make.
LIGHTHOUSE_BLOCKING_AUDITS = frozenset(
    a.strip() for a in os.environ.get("LIGHTHOUSE_BLOCKING_AUDITS", "color-contrast").split(",") if a.strip()
)

# Operator kill-switch for the whole application-auth enforcement chain (prompt segments + the
# e2e auth gate). On by default; "0" disables everything auth-related without touching per-repo
# settings -- the escape hatch for a deployment where the gate misbehaves.
AIDW_AUTH_GATE = os.environ.get("AIDW_AUTH_GATE", "1").strip().lower() not in ("0", "false", "no", "off", "")

# make_verify_node's stall-detector (graph.py's _detect_verify_stall): resets the draft session
# after this many consecutive verify laps report near-identical feedback, an unchanged
# changed_paths set, or non-improving coverage (whichever signals apply to the stage), on top of
# the existing fabrication/skipped-skill triggers. Operational kill-switch if the heuristic
# misfires -- see infra_retry.py's own env vars for the matching draft/audit-side knob.
VERIFY_STALL_LAPS = int(os.environ.get("AIDW_VERIFY_STALL_LAPS", "2"))

# Deterministic-verify verdicts that carry report["infra_error"] (the harness could not produce
# evidence -- e.g. ac_coverage_gate's test-run tee/artifacts missing) burn THIS budget instead of
# the stage's max_verify_cycles: the draft didn't fail a check, the platform failed to check.
# Observed live (2026-08-30, greenfield angular-dotnet): identical infra verdicts consumed real
# verify laps until halt. On exhaustion the run escalates as failure_type="infra_transient".
VERIFY_INFRA_RETRY_CAP = int(os.environ.get("AIDW_VERIFY_INFRA_RETRY_CAP", "2"))

# Bounded retry when a sandbox container starts but its CLI tool (whichever provider's --
# `claude --version`/`copilot --version`, per sandbox/provider.py's wait_for_cli_ready) never
# responds within that function's own readiness deadline -- distinguishes "the container is slow"
# (worth retrying) from "the container never came up" (retrying the same dead process is just spent
# time). Doc rot fix (Phase E audit M-8): this used to describe the retired SDK-based `copilot
# --server` connect handshake and its wait_for_copilot_ready check, both fully removed by the
# per-turn CLI-exec rewrite (see sandbox/provider.py's own module docstring). See
# sandbox/local_docker.py's provision().
SANDBOX_PROVISION_RETRY_ATTEMPTS = int(os.environ.get("AIDW_SANDBOX_PROVISION_RETRY_ATTEMPTS", "2"))

# How long a single `docker <args>` call (sandbox/local_docker.py's _run_docker) may run before
# it's treated as wedged and killed. Covers routine admin commands (inspect/rm/stop/start/cp/
# exec) that only talk to the local daemon. Matters more than an ordinary timeout would suggest:
# provision()/_try_reattach() hold LocalDockerProvider's one shared, non-reentrant self._lock
# while calling this, so a wedged call there freezes every OTHER session's provisioning/touch/
# liveness too, not just the stuck one.
SANDBOX_DOCKER_TIMEOUT_SECONDS = int(os.environ.get("AIDW_SANDBOX_DOCKER_TIMEOUT_SECONDS", "30"))

# For docker operations that are legitimately allowed to run long and shouldn't share the
# fast-admin default above: `docker create` can trigger a first-time image pull over the network,
# and reading a finished turn's full stdout/stderr back (cli_agent_exec.py's post-completion
# `cat` calls) can plausibly be megabytes (see TurnTimeout's own comment on turn output size).
SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS = int(
    os.environ.get("AIDW_SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS", "600")
)

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
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/anthropics-claude-plugins-official/frontend-design",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/anthropics-claude-plugins-official/code-review",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/anthropics-claude-plugins-official/code-simplifier",
    f"{COPILOT_PLUGIN_ROOT_IN_CONTAINER}/vendor/mattpocock-skills/mattpocock-skills",
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

# Timeout for CLI-based provider turns (both Claude Code and GitHub Copilot, per-turn subprocess
# exec inside the sandbox). Generous default since the agent's turn may involve multiple tool
# calls, waiting for user input/approval, or complex reasoning -- the timeout is a runaway
# backstop, not an expected exit.
CLI_AGENT_TURN_TIMEOUT_SECONDS = int(os.environ.get("CLI_AGENT_TURN_TIMEOUT_SECONDS", "2400"))

# Skills each stage is REQUIRED to invoke, enforced deterministically rather than trusted: the
# stage's prompt names them, and gates/skill_gate.py verifies via chat_model's provider dispatch
# (get_session_id + read_skill_invocations) -- which means different things per provider. Claude's
# implementation reads that session's real CLI transcript and works; Copilot's unconditionally
# returns None (no CLI-exec equivalent exists yet to the old SDK-server session log this used to
# read), so verification is permanently unavailable under the default provider today -- see
# skill_gate.py's own module docstring. Self-report (StageReport.skills_invoked) is telemetry, not
# evidence regardless -- a model that skipped a skill will happily claim it used one.
REQUIRED_SKILLS_BY_STAGE: dict[str, list[str]] = {
    # grill-me (mattpocock pack, vendored in the sandbox image): the spec prompt has always asked
    # for it; required here after a live run (2026-08-31) shipped a spec with zero Skill calls --
    # the gate is what closes the prompt-says/agent-skips gap.
    "specification": ["brainstorming", "grill-me"],
    "plan": ["writing-plans"],
    "ac-to-tests": ["test-driven-development"],
    # ponytail: minimal_code_to_green_draft.md has mandated it for as long as the prompt existed --
    # requiring it here just closes the prompt-says/gate-checks gap the skill gate exists for.
    # code-review: the Claude CLI's BUILT-IN code-review skill (2.1.x bundles it; commands unified
    # into the Skill tool, so it shows up in the transcript like any other skill). The vendored
    # anthropics code-review PLUGIN also loads, but its command body is gh-PR-hardwired and fans
    # out ~10 subagents -- the built-in reviews the working tree diff directly. See
    # gates/skill_gate.py's known-set assert and the prompt mandate in
    # minimal_code_to_green_draft.md.
    "minimal-code-to-green": [
        "executing-plans",
        "requesting-code-review",
        "verification-before-completion",
        "ponytail",
        "code-review",
    ],
    # agent:code-simplifier -- the "agent:" prefix means a Task-tool subagent launch, not a Skill
    # invocation (see claude_chat_model.read_skill_invocations' naming scheme). Requires
    # builtin:task in remediation's available_tools (graph.py session_options).
    # security-review: the diff-based security pass (built-in skill; the vendored awesome-copilot
    # skill answers to the same name -- either satisfies the gate). Restores the P10-era mandate
    # that was lost when the security stage consolidated into remediation.
    "remediation": ["agent:code-simplifier", "security-review"],
    "adversarial-compliance": ["receiving-code-review", "verification-before-completion"],
    "metrics-exit": ["finishing-a-development-branch"],
    # dispatching-parallel-agents is deliberately NOT required: it applies only when the plan has
    # genuinely independent steps, so mandating it would force a nonsense invocation on a linear
    # plan. systematic-debugging likewise -- the fix nodes it belongs to only run on failure.
    # The mattpocock skills (grill-me, grill-with-docs, diagnosing-bugs,
    # improve-codebase-architecture) and frontend-design are prompt-ENCOURAGED, not required:
    # the grill-* pair is interactive by nature, frontend-design only applies to UI repos (this
    # static map cannot express that), and promotion to required is telemetry-driven from the
    # skills evidence each run persists.
}

# Read-only tool allowlist (Phase A0 spike finding: excluded_tools blocklisting write-capable
# tools is incomplete -- the model can reach create/bash/edit/apply_patch interchangeably, so
# read-only stages must allowlist via available_tools instead). All entries are source-qualified
# ("builtin:<name>") per copilot._mode.ToolSet -- bare names are rejected/silently ignored.
READ_ONLY_AVAILABLE_TOOLS = [
    "builtin:view",
    "builtin:grep",
    "builtin:glob",
    # builtin:task_complete deliberately excluded (2026-09-04): every one of this list's 10 call
    # sites is a structured-output turn (ainvoke_structured, directly or via StageSpec.
    # response_schema), and offering this tool let the model end the turn with plain
    # "Task complete: ..." prose instead of the required JSON -- structured_output.py's
    # model_validate_json then rejected it, burned all 3 retries on a generic parse error, and
    # killed the stage. ac-to-tests' own draft tool list never included it and works fine, proving
    # it's optional, not required, for a Copilot CLI turn to terminate cleanly.
    "builtin:ask_user",
    "builtin:skill",
]
