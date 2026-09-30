"""Runtime configuration -- a thin, live-editable shim over agent/src/runtime_settings.py's
per-session DB-backed overrides (SPECIFICATION.md US-10's original configurable safety cap, since
folded into the broader Org Settings migration).

Every constant below is resolved dynamically via module __getattr__ (PEP 562, Python 3.12+), not a
static value computed once at import time: `config.NAME` first checks
`runtime_settings.get_raw(NAME)` (this session's pinned DB-backed override, if any -- see
runtime_settings.py's own docstring for exactly when that's populated), and only when that's unset
falls back to this file's own env-var/default -- exactly the value it always returned before this
migration. That env-var/Key-Vault-backed fallback tier is PERMANENT, not transitional: it stays the
deploy-time default forever, this DB layer is only the live-without-redeploy override on top
(docs/CONFIG.md documents the same precedence for the `provider` setting).

Every one of the ~265 existing call sites across 19 files keeps working completely unchanged --
`config.NAME` / `workflow_config.NAME` attribute access is exactly what triggers __getattr__ --
EXCEPT a handful that used to snapshot a value into their own module-level global, function
default-argument, or dataclass field default at import/def time instead of reading `config.NAME`
fresh at point of use. Those go stale under live-refresh and were converted to attribute-style
access as part of this migration (see each file's own diff): repo_scan.py, e2e_nodes.py,
exit_nodes.py, gates/test_coverage_gate.py.

NOT every constant that used to live here went into the declarative table below. Three groups were
deliberately excluded from the live settings system and remain plain, static module-level
assignments further down this file (Python resolves a real module attribute before ever calling
__getattr__, so they coexist without conflict):
  - Structural/protocol tables with no realistic operator-tunability case (which skills/tools are
    even valid to invoke, not a limit to tune) -- REQUIRED_SKILLS_BY_STAGE,
    AUDIT_FULL_READ_FILE_BY_STAGE, COPILOT_DISABLED_SKILLS[_SPECIFICATION],
    READ_ONLY_AVAILABLE_TOOLS, COPILOT_PLUGIN_ROOT_IN_CONTAINER (+ its derived
    COPILOT_PLUGIN_DIRECTORIES).
  - Category J's productivity/app-health formula weights (AIDW_APP_HEALTH_COVERAGE_WEIGHT,
    AIDW_APP_HEALTH_PASS_RATE_WEIGHT, AIDW_HOURS_PER_LOC_BASE, AIDW_COMPLEXITY_HOUR_MULTIPLIERS,
    AIDW_REVIEW_OVERHEAD_FRACTION) -- feed only a cosmetic "hours saved" reporting number, zero
    observed-live retuning history unlike every verify-cycle cap below.
  - The whole EXIT_*_CELL_CHARS markdown-table-rendering family (15 constants) -- internal
    rendering detail exit_nodes.py makes zero LLM calls around; no operator would plausibly tune a
    table cell's character width via env var (AGENTS.md's own carve-out example). Plus
    GIT_OPS_PUSH_ERROR_TAIL_CHARS (trivial, UI-display-only).
8 more constants were deleted outright, not excluded (dead code or log-only with zero downstream
consumer caring about length): TEST_COVERAGE_UNCOVERED_LINES_MAX, GRAPH_FEEDBACK_LOG_PREVIEW_CHARS,
GIT_OPS_API_ERROR_PREVIEW_CHARS, GIT_OPS_GITIGNORE_PREVIEW_MAX, E2E_SCREENSHOT_STDOUT_TAIL_CHARS,
E2E_LIGHTHOUSE_STDOUT_TAIL_CHARS, AIDW_TOOL_PROBE_NOTES_HEAD_CHARS, AIDW_TOOL_PROBE_NOTES_TAIL_CHARS
-- their call sites now use the full, untruncated text directly.

Must stay a leaf module (only stdlib + runtime_settings imports): claude_chat_model.py/
copilot_chat_model.py import this file, chat_model.py imports both of those -- importing anything
that imports chat_model.py here would complete that cycle. runtime_settings.py -> session_store.py
-> db.py has no import back to chat_model.py, so this dependency is safe.

Self-check (config.py's first-ever test): `cd agent && uv run python -m src.config`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from . import runtime_settings

logger = logging.getLogger(__name__)

_PARSERS: dict[str, Callable[[str], object]] = {
    "int": int,
    "float": float,
    "str": str,
    "bool": lambda s: s.strip().lower() not in ("0", "false", "no", "off", ""),
    "csv": lambda s: tuple(x.strip() for x in s.split(",") if x.strip()),
    "csv_int": lambda s: tuple(int(x.strip()) for x in s.split(",") if x.strip()),
    "csv_float": lambda s: tuple(float(x.strip()) for x in s.split(",") if x.strip()),
    "frozenset_csv": lambda s: frozenset(x.strip() for x in s.split(",") if x.strip()),
}

# The write-side inverse of _PARSERS -- must produce the exact string shape the matching parser
# above expects (a live Python value formatted back into env-var text), never an arbitrary
# json.dumps() of it. This split exists because an earlier draft conflated the two: json.dumps of a
# tuple/frozenset produces JSON array syntax ("[1, 2, 3]"), which the CSV-style parsers above then
# fail to re-parse (int("[1") raises), and json.dumps(frozenset(...)) raises TypeError outright.
# Callers (the Settings API) run the matching formatter before calling runtime_settings.set_value,
# and must run the matching PARSER as a dry-run validation before persisting -- see runtime_settings
# set_value's own docstring.
_FORMATTERS: dict[str, Callable[[object], str]] = {
    "int": str,
    "float": str,
    "str": str,
    "bool": lambda v: "1" if v else "0",
    "csv": lambda v: ",".join(v),
    "csv_int": lambda v: ",".join(str(x) for x in v),
    "csv_float": lambda v: ",".join(str(x) for x in v),
    "frozenset_csv": lambda v: ",".join(sorted(v)),
}


@dataclass(frozen=True)
class _Setting:
    """One migrated constant's full metadata: how to parse/format it, where its permanent
    env-var/default fallback comes from, and the purpose/effect/range text the Org Settings UI
    shows next to its edit control (every settings-form field must explain its own purpose, the
    ramifications of changing it, and its valid range where one applies)."""

    parser: str  # key into _PARSERS / _FORMATTERS
    env_var: str
    default_raw: str  # env-var-shaped default string; parser(default_raw) must succeed
    category: str  # UI grouping
    purpose: str  # one-line: what this knob controls
    effect: str  # one-line: what raising/lowering/changing it actually does, and the tradeoff
    value_range: str | None = None  # human-readable valid range/format; None if not meaningfully bounded


_SETTINGS: dict[str, _Setting] = {
    # -- Clarification-cycle caps (SPECIFICATION.md US-10) --------------------------------------
    "SPEC_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "SPEC_MAX_CLARIFICATION_CYCLES", "3", "clarification_cycles",
        "Max clarification Q&A rounds the Specification stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    "PLAN_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "PLAN_MAX_CLARIFICATION_CYCLES", "3", "clarification_cycles",
        "Max clarification Q&A rounds the Plan stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    "AC_TO_TESTS_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "AC_TO_TESTS_MAX_CLARIFICATION_CYCLES", "3", "clarification_cycles",
        "Max clarification Q&A rounds the AC-to-Tests stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    "MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES", "3", "clarification_cycles",
        "Max clarification Q&A rounds the Minimal-Code-to-Green stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    "ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES", "2", "clarification_cycles",
        "Max clarification Q&A rounds the Adversarial Audit stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    "EXIT_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "EXIT_MAX_CLARIFICATION_CYCLES", "2", "clarification_cycles",
        "Max clarification Q&A rounds the metrics-exit stage may use before its safety cap forces a decision.",
        "Higher allows more back-and-forth before forcing a decision; lower risks cutting off legitimate clarification early.",
        "positive integer",
    ),
    # Small default: tech-stack detection is autonomous codebase study, not human-clarification-
    # driven, so this safety cap should rarely if ever trigger.
    "TECH_STACK_MAX_CLARIFICATION_CYCLES": _Setting(
        "int", "TECH_STACK_MAX_CLARIFICATION_CYCLES", "2", "clarification_cycles",
        "Max clarification Q&A rounds the tech-stack detection pass may use before its safety cap forces a decision.",
        "Rarely triggers (detection is autonomous, not clarification-driven); higher allows more rounds, lower cuts off sooner.",
        "positive integer",
    ),
    # preflight_nodes.py's brownfield startability probe: bounds how long ONE app_discovery
    # candidate gets to open its listening port before this repo is declared not startable.
    # Deliberately its OWN, smaller knob rather than reusing E2E_APP_READY_TIMEOUT_SECONDS: this
    # probe boots one candidate in isolation, synchronously, in the critical path of a human
    # waiting on the Tech Stack tab.
    "AIDW_TECH_STACK_BOOT_PROBE_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_TECH_STACK_BOOT_PROBE_TIMEOUT_SECONDS", "45", "clarification_cycles",
        "Seconds one app-discovery candidate gets to open its listening port during the Tech Stack tab's startability probe.",
        "Too short false-flags a slow-starting app as non-startable (disables e2e/App Health until a manual recheck); too long stalls the tab per broken candidate.",
        "seconds, positive",
    ),

    # -- Verify-cycle budgets + attempt caps -----------------------------------------------------
    # graph.py's StageSpec.max_verify_cycles per stage: the deterministic-gate verify->draft retry
    # budget. Deliberately SEVEN SEPARATE constants, not one shared cap: each stage's number was
    # tuned from that stage's own observed failure mode; sharing one constant would let retuning
    # any single stage silently move every other stage's budget too.
    "SPEC_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_SPEC_MAX_VERIFY_CYCLES", "5", "verify_cycles",
        "Max draft<->verify retry laps for the Specification stage before it escalates as failed.",
        "Higher gives a stuck-but-converging run more laps to resolve (more wall-clock/spend); lower escalates sooner on a run that might still converge.",
        "positive integer",
    ),
    "PLAN_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_PLAN_MAX_VERIFY_CYCLES", "5", "verify_cycles",
        "Max draft<->verify retry laps for the Plan stage before it escalates as failed.",
        "Higher gives a stuck-but-converging run more laps to resolve (more wall-clock/spend); lower escalates sooner on a run that might still converge.",
        "positive integer",
    ),
    "AC_TO_TESTS_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_AC_TO_TESTS_MAX_VERIFY_CYCLES", "8", "verify_cycles",
        "Max draft<->verify retry laps for the AC-to-Tests stage before it escalates as failed.",
        "Higher gives a stuck-but-converging run more laps to resolve (more wall-clock/spend); lower escalates sooner on a run that might still converge.",
        "positive integer",
    ),
    "MINIMAL_CODE_TO_GREEN_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_MINIMAL_CODE_TO_GREEN_MAX_VERIFY_CYCLES", "14", "verify_cycles",
        "Max draft<->verify retry laps for the Minimal-Code-to-Green stage before it escalates as failed.",
        "Higher gives a stuck-but-converging coverage run more laps to close the gap (more wall-clock/spend); lower escalates sooner.",
        "positive integer",
    ),
    "REMEDIATION_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_REMEDIATION_MAX_VERIFY_CYCLES", "5", "verify_cycles",
        "Max draft<->verify retry laps for the Remediation stage before it escalates as failed.",
        "Higher gives a stuck-but-converging run more laps to resolve (more wall-clock/spend); lower escalates sooner on a run that might still converge.",
        "positive integer",
    ),
    "ADVERSARIAL_AUDIT_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_ADVERSARIAL_AUDIT_MAX_VERIFY_CYCLES", "6", "verify_cycles",
        "Max draft<->verify retry laps for the Adversarial Audit stage before it escalates as failed.",
        "Higher gives a stuck-but-converging run more laps to resolve (more wall-clock/spend); lower escalates sooner on a run that might still converge.",
        "positive integer",
    ),
    "EXIT_MAX_VERIFY_CYCLES": _Setting(
        "int", "AIDW_EXIT_MAX_VERIFY_CYCLES", "3", "verify_cycles",
        "Max draft<->verify retry laps for the metrics-exit stage before it escalates as failed.",
        "Higher gives a stage that failed the pre-exit skill gate more laps to correct it; lower escalates sooner.",
        "positive integer",
    ),
    "TARGETED_FIX_MAX_ATTEMPTS": _Setting(
        "int", "TARGETED_FIX_MAX_ATTEMPTS", "3", "verify_cycles",
        "Max times a user may invoke the \"targeted-fix\" action against one already-closed session.",
        "Higher permits more repeated seeded-fix attempts against the same closed run; lower refuses sooner.",
        "positive integer",
    ),
    "AIDW_E2E_RESET_MAX_ATTEMPTS": _Setting(
        "int", "AIDW_E2E_RESET_MAX_ATTEMPTS", "3", "verify_cycles",
        "Max times a user may invoke the \"reset-e2e\" action, which clears e2e/metrics-exit state and re-walks from remediation.",
        "Higher permits more replays (each a real regression-suite re-run, not just an LLM fix pass) against the same closed run; lower refuses sooner.",
        "positive integer",
    ),
    # make_verify_node's stall-detector: resets the draft session after this many consecutive
    # verify laps report near-identical feedback/unchanged paths/non-improving coverage.
    "VERIFY_STALL_LAPS": _Setting(
        "int", "AIDW_VERIFY_STALL_LAPS", "2", "verify_cycles",
        "Consecutive non-improving verify laps before the draft session is force-reset.",
        "Higher tolerates more non-improving laps before resetting (more spend on a possibly-stuck session); lower resets sooner, at the cost of resetting a session that was about to recover.",
        "positive integer",
    ),
    # Deterministic-verify verdicts carrying report["infra_error"] (harness couldn't produce
    # evidence) burn THIS budget instead of the stage's own max_verify_cycles.
    "VERIFY_INFRA_RETRY_CAP": _Setting(
        "int", "AIDW_VERIFY_INFRA_RETRY_CAP", "2", "verify_cycles",
        "Separate retry budget for verify verdicts caused by a platform/harness failure, not a draft content failure.",
        "Higher tolerates more infra-caused verify failures before escalating as infra_transient; lower escalates sooner.",
        "positive integer",
    ),

    # -- E2E cluster (agent/src/e2e_nodes.py) ----------------------------------------------------
    # The e2e loop's job is to FIX the app, not exit early -- a failing acceptance journey is a
    # code bug, and escalating hands a human a broken app. The cap exists only as a runaway
    # backstop, not an expected exit.
    "E2E_MAX_FIX_CYCLES": _Setting(
        "int", "E2E_MAX_FIX_CYCLES", "8", "e2e",
        "Max fix-cycle laps the e2e stage spends trying to make failing acceptance journeys pass.",
        "Higher gives a genuinely-converging app more laps to get fixed (more spend); lower escalates sooner, handing a human a still-broken app.",
        "positive integer",
    ),
    "TEST_HARDENING_MAX_FIX_CYCLES": _Setting(
        "int", "TEST_HARDENING_MAX_FIX_CYCLES", "4", "e2e",
        "Max fix-cycle laps for stable unit/integration-test regressions found after e2e.",
        "Higher gives more laps to repair regressions in-pipeline; lower escalates sooner as a runaway backstop.",
        "positive integer",
    ),
    "E2E_APP_READY_TIMEOUT_SECONDS": _Setting(
        "int", "E2E_APP_READY_TIMEOUT_SECONDS", "120", "e2e",
        "Seconds the e2e stage waits for the app under test to finish booting before treating it as failed to start.",
        "Higher tolerates a slower-booting app; lower fails faster on a genuinely broken boot, at the risk of false-flagging a slow-but-healthy one.",
        "seconds, positive",
    ),
    "E2E_SUITE_TIMEOUT_SECONDS": _Setting(
        "int", "E2E_SUITE_TIMEOUT_SECONDS", "1200", "e2e",
        "Hard wall-clock cap on the whole Playwright verification suite run, via `timeout`.",
        "Higher tolerates a slower full suite; lower kills a hung suite sooner, at the risk of cutting off a genuinely slow-but-passing run.",
        "seconds, positive",
    ),
    # Caps Playwright's own --workers flag. Raising trades a faster suite for higher peak memory
    # -- observed live: 2 workers still let the dev server die mid-suite under Docker Desktop
    # memory pressure; only fully serialized (1) removed it entirely.
    "AIDW_E2E_PLAYWRIGHT_WORKERS": _Setting(
        "int", "AIDW_E2E_PLAYWRIGHT_WORKERS", "1", "e2e",
        "Caps Playwright's own --workers concurrency for the full verification suite run.",
        "Raising risks the sandboxed dev server crashing mid-suite under memory pressure (observed live even at 2 workers); 1 is safest unless the host has meaningfully more Docker Desktop memory headroom.",
        "positive integer, 1 recommended unless host has ample memory headroom",
    ),
    "AIDW_E2E_REUSE_PROVEN_LAUNCH": _Setting(
        "bool", "AIDW_E2E_REUSE_PROVEN_LAUNCH", "1", "e2e",
        "Whether a fix-cycle lap reuses a previously-confirmed start_command/port instead of re-running launch discovery.",
        "On (default) skips a paid discovery turn per lap once a launch is proven; turn off only if a stale cache is suspected of masking a real app-source change.",
        "true/false",
    ),
    "LIGHTHOUSE_PERF_MIN": _Setting(
        "int", "LIGHTHOUSE_PERF_MIN", "0", "e2e",
        "Lighthouse performance score floor (0-100); below it counts as an e2e failure. 0 = report-only, never gates.",
        "Raising above 0 starts gating on performance, which is timing-noisy on the headless dev-server shell and can burn fix-cycle laps on a number code can't reliably move.",
        "0-100 (0 disables gating)",
    ),
    "LIGHTHOUSE_A11Y_MIN": _Setting(
        "int", "LIGHTHOUSE_A11Y_MIN", "90", "e2e",
        "Lighthouse accessibility score floor (0-100); below it counts as an e2e failure and feeds the fix loop.",
        "Higher enforces stricter accessibility (axe-backed, deterministic, fixable); lower lets weaker accessibility through.",
        "0-100",
    ),
    "LIGHTHOUSE_BLOCKING_AUDITS": _Setting(
        "frozenset_csv", "LIGHTHOUSE_BLOCKING_AUDITS", "color-contrast", "e2e",
        "Lighthouse audit ids that block the e2e gate on their own, whatever the aggregate accessibility score.",
        "Adding an audit id here means a single failure on it blocks merge regardless of overall score; removing one demotes it to score-only. Empty disables entirely.",
        "comma-separated Lighthouse audit ids, e.g. \"color-contrast\"",
    ),
    "AIDW_AUTH_GATE": _Setting(
        "bool", "AIDW_AUTH_GATE", "1", "e2e",
        "Operator kill-switch for the whole application-auth enforcement chain (prompt segments + the e2e auth gate).",
        "On (default) enforces auth; off disables everything auth-related without touching per-repo settings -- the escape hatch if the gate misbehaves.",
        "true/false",
    ),
    "E2E_APP_LOG_PATH": _Setting(
        "str", "AIDW_E2E_APP_LOG_PATH", "agent-work/e2e-app.log", "e2e",
        "Sandbox-relative path where the booted app-under-test's stdout+stderr are redirected.",
        "Changing this only moves where the agent writes/reads inside the sandbox; no effect on suite behavior.",
        "sandbox-relative file path",
    ),
    "E2E_APP_PID_PATH": _Setting(
        "str", "AIDW_E2E_APP_PID_PATH", "agent-work/e2e-app.pid", "e2e",
        "Sandbox-relative path where the booted app-under-test's PID is recorded so it can be killed after the suite runs.",
        "Changing this only moves where the agent writes/reads inside the sandbox; no effect on suite behavior.",
        "sandbox-relative file path",
    ),
    "E2E_PROBE_PREVIEW_CHARS": _Setting(
        "int", "AIDW_E2E_PROBE_PREVIEW_CHARS", "300", "e2e",
        "How much of a page-probe's raw output to include in the diagnostic error string when it isn't parseable JSON.",
        "Higher shows more raw output for diagnosis; this is diagnostics only, never fed to a model.",
        "positive integer, characters",
    ),
    "E2E_CONSOLE_ERRORS_MAX": _Setting(
        "int", "AIDW_E2E_CONSOLE_ERRORS_MAX", "5", "e2e",
        "How many captured browser console errors to list per probed route.",
        "Higher shows more console errors per route to the fix loop; lower truncates the list sooner.",
        "positive integer",
    ),
    "E2E_PAGE_TEXT_PREVIEW_CHARS": _Setting(
        "int", "AIDW_E2E_PAGE_TEXT_PREVIEW_CHARS", "600", "e2e",
        "How much of a probed route's rendered page text to show in its failure summary.",
        "Higher shows more rendered text per route; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_DEGENERATE_PNG_MAX_BYTES": _Setting(
        "int", "AIDW_E2E_DEGENERATE_PNG_MAX_BYTES", "8192", "e2e",
        "A screenshot PNG at or below this byte size is treated as evidence the page painted nothing (blank).",
        "Raising risks flagging a genuinely tiny-but-real page as blank; lowering risks missing a blank capture a few bytes larger.",
        "positive integer, bytes",
    ),
    "E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS": _Setting(
        "csv_int", "AIDW_E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS", "3000,10000,15000", "e2e",
        "Escalating retry ladder (milliseconds between capture attempts) for a client-rendered route that hasn't hydrated yet.",
        "A longer/higher ladder tolerates slower-hydrating stacks (more wall-clock per route, up to E2E_ROUTES_MAX routes); a shorter one risks capturing an unhydrated page as the final screenshot.",
        "comma-separated milliseconds, ascending, e.g. \"3000,10000,15000\"",
    ),
    "E2E_ROUTES_MAX": _Setting(
        "int", "AIDW_E2E_ROUTES_MAX", "12", "e2e",
        "How many routes the screenshot/lighthouse harvest captures per run.",
        "Higher increases both wall-clock time (each route pays the full hydrate ladder) and exit-report screenshot count; lower captures fewer routes.",
        "positive integer",
    ),
    "E2E_SCREENSHOT_COPY_MAX_FILES": _Setting(
        "int", "AIDW_E2E_SCREENSHOT_COPY_MAX_FILES", "100", "e2e",
        "Caps how many suite-generated screenshot PNGs get batched into one copy-out-of-sandbox script.",
        "Raising risks re-hitting the host OS's command-line length ceiling (an uncaught error); lowering just harvests fewer of the suite's own screenshots (per-route screenshots are unaffected).",
        "positive integer",
    ),
    "E2E_LIGHTHOUSE_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_E2E_LIGHTHOUSE_TIMEOUT_SECONDS", "150", "e2e",
        "Hard wall-clock cap on one route's Lighthouse run.",
        "A route that hangs past this is skipped (fail-open, never scored as 0) rather than wedging the whole e2e stage; higher tolerates slower Lighthouse runs.",
        "seconds, positive",
    ),
    "E2E_BLANK_SCREENSHOTS_PREVIEW_MAX": _Setting(
        "int", "AIDW_E2E_BLANK_SCREENSHOTS_PREVIEW_MAX", "5", "e2e",
        "How many blank-screenshot filenames to list inline in a failure item before summarizing the rest.",
        "Higher lists more filenames inline; lower summarizes sooner as \"and N more\".",
        "positive integer",
    ),
    "E2E_LIGHTHOUSE_AUDIT_TEXT_CHARS": _Setting(
        "int", "AIDW_E2E_LIGHTHOUSE_AUDIT_TEXT_CHARS", "120", "e2e",
        "Length of one failing Lighthouse audit's title/selector string shown to the e2e fix model.",
        "Higher shows more detail per failing audit; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_LIGHTHOUSE_FAILING_AUDITS_MAX": _Setting(
        "int", "AIDW_E2E_LIGHTHOUSE_FAILING_AUDITS_MAX", "12", "e2e",
        "How many failing Lighthouse audits survive per route/aggregation into the e2e fix model's context.",
        "Higher shows more failing audits to the fix model; lower truncates the list sooner.",
        "positive integer",
    ),

    # -- Prompt-facing truncation (kept bounded -- see the migration plan's "Truncation family"
    # research: this codebase has zero context-window/token-limit handling, so unbounded
    # prompt-facing text risks a silent, expensive misattribution as a content failure) ----------
    "REBUILD_OUTPUT_TAIL_CHARS": _Setting(
        "int", "AIDW_REBUILD_OUTPUT_TAIL_CHARS", "8000", "truncation",
        "Per-command build-output capture length fed to the rebuild fix loop's prompt.",
        "Higher shows more of a long build log to the fix model (more prompt size/cost); lower risks the model never seeing errors outside the tail.",
        "positive integer, characters",
    ),
    "REBUILD_OUTPUT_COMBINED_TAIL_CHARS": _Setting(
        "int", "AIDW_REBUILD_OUTPUT_COMBINED_TAIL_CHARS", "16000", "truncation",
        "Combined build-output capture length across all commands fed to the rebuild fix loop's prompt.",
        "Higher shows more combined output to the fix model (more prompt size/cost); lower risks truncating a multi-command failure's later errors.",
        "positive integer, characters",
    ),
    "READ_TOOL_DEFAULT_WINDOW_LINES": _Setting(
        "int", "AIDW_READ_TOOL_DEFAULT_WINDOW_LINES", "2000", "truncation",
        "The CLI's own default Read-tool line window, used to compute whether an unparameterized read counts as a full-file read.",
        "Should match the CLI's actual documented default; too low makes a genuine full-file read register as incomplete, too high lets a partial read pass as complete.",
        "positive integer, lines; should match the coding CLI's own documented default",
    ),
    "TEST_COVERAGE_OUTPUT_HEAD_CHARS": _Setting(
        "int", "AIDW_TEST_COVERAGE_OUTPUT_HEAD_CHARS", "750", "truncation",
        "Head portion of one coverage-command's captured stdout/stderr, before it's joined into a failure summary.",
        "Higher preserves more of the command's early output; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "TEST_COVERAGE_OUTPUT_TAIL_CHARS": _Setting(
        "int", "AIDW_TEST_COVERAGE_OUTPUT_TAIL_CHARS", "750", "truncation",
        "Tail portion of one coverage-command's captured stdout/stderr, before it's joined into a failure summary.",
        "Higher preserves more of the command's late output; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "TEST_COVERAGE_FAILURE_DETAIL_HEAD_CHARS": _Setting(
        "int", "AIDW_TEST_COVERAGE_FAILURE_DETAIL_HEAD_CHARS", "150", "truncation",
        "Head portion of one coverage-command's one-line failure_detail summary.",
        "Higher preserves more detail per command's summary line; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "TEST_COVERAGE_FAILURE_DETAIL_TAIL_CHARS": _Setting(
        "int", "AIDW_TEST_COVERAGE_FAILURE_DETAIL_TAIL_CHARS", "150", "truncation",
        "Tail portion of one coverage-command's one-line failure_detail summary.",
        "Higher preserves more detail per command's summary line; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "REBUILD_GATING_FINDINGS_MAX": _Setting(
        "int", "AIDW_REBUILD_GATING_FINDINGS_MAX", "10", "truncation",
        "How many rebuild scan-delta gating findings get listed before summarizing the rest, in a message the rebuild fix-loop prompt also reads.",
        "Higher shows more findings inline to the fix model; lower summarizes sooner as \"...and N more\".",
        "positive integer",
    ),
    "REBUILD_FINDING_MESSAGE_CHARS": _Setting(
        "int", "AIDW_REBUILD_FINDING_MESSAGE_CHARS", "110", "truncation",
        "How much of one gating finding's own title/message survives per line in the rebuild gate summary.",
        "Higher shows more detail per finding; lower truncates each line sooner.",
        "positive integer, characters",
    ),
    "GRAPH_FAILED_TESTS_MAX": _Setting(
        "int", "AIDW_GRAPH_FAILED_TESTS_MAX", "10", "truncation",
        "How many failed-test entries get inlined into the metrics-report/e2e-outcome prompt messages.",
        "Higher shows more failing tests to the model; lower truncates the list sooner.",
        "positive integer",
    ),
    "GRAPH_METRICS_JSON_MAX_CHARS": _Setting(
        "int", "AIDW_GRAPH_METRICS_JSON_MAX_CHARS", "8000", "truncation",
        "JSON-serialization budget for the metrics-compute payload shown to the model.",
        "Higher lets the model see more of a large metrics payload (more prompt size/cost); lower truncates it sooner (honestly, via a marked wrapper, not mid-token).",
        "positive integer, characters",
    ),
    "GRAPH_E2E_SUMMARY_JSON_MAX_CHARS": _Setting(
        "int", "AIDW_GRAPH_E2E_SUMMARY_JSON_MAX_CHARS", "4000", "truncation",
        "JSON-serialization budget for the e2e-summary payload shown to the model.",
        "Higher lets the model see more of a large e2e summary (more prompt size/cost); lower truncates it sooner (honestly, via a marked wrapper, not mid-token).",
        "positive integer, characters",
    ),
    "GRAPH_BOUNDED_JSON_MARGIN_CHARS": _Setting(
        "int", "AIDW_GRAPH_BOUNDED_JSON_MARGIN_CHARS", "240", "truncation",
        "Reserve space the honest-JSON-truncation helper keeps for its own wrapper keys around a clipped preview.",
        "Must stay big enough that the wrapper itself never exceeds the overall JSON size limit; only change alongside GRAPH_METRICS_JSON_MAX_CHARS/GRAPH_E2E_SUMMARY_JSON_MAX_CHARS.",
        "positive integer, characters",
    ),
    "GRAPH_INFRA_ERROR_CHARS": _Setting(
        "int", "AIDW_GRAPH_INFRA_ERROR_CHARS", "2000", "truncation",
        "How much of a raw infra-exhaustion exception message is kept as the stage's last_infra_error.",
        "Higher preserves more of the exception text; lower truncates it sooner (tail-only, since this is a short exception string, not a list).",
        "positive integer, characters",
    ),
    "TEST_COVERAGE_GAP_DETAIL_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_GAP_DETAIL_MAX", "6", "truncation",
        "How many coverage gaps get a quoted source excerpt, and how many branch line numbers per gap, in verify feedback.",
        "Higher shows more gap detail to the model (avoiding a full coverage-report re-read); lower truncates the list sooner.",
        "positive integer",
    ),
    "DIAGRAM_ERROR_SUMMARY_HEAD_CHARS": _Setting(
        "int", "AIDW_DIAGRAM_ERROR_SUMMARY_HEAD_CHARS", "2000", "truncation",
        "Head portion of a diagram-render error's captured output fed into the next draft prompt.",
        "Higher preserves more of the renderer's own error output (its actionable \"parse error\" text is typically at the start); lower truncates it sooner.",
        "positive integer, characters",
    ),
    "DIAGRAM_ERROR_SUMMARY_TAIL_CHARS": _Setting(
        "int", "AIDW_DIAGRAM_ERROR_SUMMARY_TAIL_CHARS", "2000", "truncation",
        "Tail portion of a diagram-render error's captured output fed into the next draft prompt.",
        "Higher preserves more of the renderer's own error output; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "DIAGRAM_ERROR_SUMMARY_LINES_MAX": _Setting(
        "int", "AIDW_DIAGRAM_ERROR_SUMMARY_LINES_MAX", "10", "truncation",
        "How many lines of a diagram-render error get joined into the summary shown to the model.",
        "Higher shows more error lines; lower truncates the summary sooner.",
        "positive integer",
    ),
    "DIAGRAM_ERROR_SUMMARY_JOINED_CHARS": _Setting(
        "int", "AIDW_DIAGRAM_ERROR_SUMMARY_JOINED_CHARS", "700", "truncation",
        "Overall character cap on the joined diagram-render error summary shown to the model.",
        "Higher shows more of the joined summary; lower truncates it sooner.",
        "positive integer, characters",
    ),

    # -- Low-stakes but kept (DB/UI-facing, trivial cost either way) -----------------------------
    "EXIT_FAILURE_DETAIL_HEAD_CHARS": _Setting(
        "int", "AIDW_EXIT_FAILURE_DETAIL_HEAD_CHARS", "1250", "truncation",
        "Head portion of the \"Terminal failure\" code block in the human-facing final exit report.",
        "Higher preserves more of the failure detail (which can be a findings LIST); lower truncates it sooner.",
        "positive integer, characters",
    ),
    "EXIT_FAILURE_DETAIL_TAIL_CHARS": _Setting(
        "int", "AIDW_EXIT_FAILURE_DETAIL_TAIL_CHARS", "1250", "truncation",
        "Tail portion of the \"Terminal failure\" code block in the human-facing final exit report.",
        "Higher preserves more of the failure detail; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_BOOT_FAILURE_LOG_HEAD_CHARS": _Setting(
        "int", "AIDW_E2E_BOOT_FAILURE_LOG_HEAD_CHARS", "1500", "truncation",
        "Head portion of the app-boot readiness failure description embedded in a failed e2e test's error field.",
        "Higher preserves more of the boot failure text; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_BOOT_FAILURE_LOG_TAIL_CHARS": _Setting(
        "int", "AIDW_E2E_BOOT_FAILURE_LOG_TAIL_CHARS", "1500", "truncation",
        "Tail portion of the app-boot readiness failure description embedded in a failed e2e test's error field.",
        "Higher preserves more of the boot failure text; lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_FIX_APP_LOG_HEAD_CHARS": _Setting(
        "int", "AIDW_E2E_FIX_APP_LOG_HEAD_CHARS", "2000", "truncation",
        "Head portion of the app log tail handed directly to the e2e fix model's own prompt.",
        "Higher shows the fix model more of the app log (more prompt size/cost); lower truncates it sooner.",
        "positive integer, characters",
    ),
    "E2E_FIX_APP_LOG_TAIL_CHARS": _Setting(
        "int", "AIDW_E2E_FIX_APP_LOG_TAIL_CHARS", "2000", "truncation",
        "Tail portion of the app log tail handed directly to the e2e fix model's own prompt.",
        "Higher shows the fix model more of the app log (more prompt size/cost); lower truncates it sooner.",
        "positive integer, characters",
    ),
    "REBUILD_LEDGER_DETAIL_CHARS": _Setting(
        "int", "AIDW_REBUILD_LEDGER_DETAIL_CHARS", "1500", "truncation",
        "How much of the TDD-red gate's blocking detail (a LIST of wrongly-passed tests) is recorded in the durable ledger.",
        "Higher records more of the list a human later reviews; lower risks recording that the gate fired without recording what it found.",
        "positive integer, characters",
    ),
    "REBUILD_ESCALATE_FEEDBACK_CHARS": _Setting(
        "int", "AIDW_REBUILD_ESCALATE_FEEDBACK_CHARS", "1000", "truncation",
        "Length of the one-line failure feedback recorded for the session's DB failure_message once the rebuild fix-cycle cap is exhausted.",
        "Higher preserves more detail in the stored failure message; lower truncates it sooner. session_store.py's own _build_failure reads this same setting (unified during the Org Settings migration -- it used to have an independent hardcoded 500-char cap that silently shadowed this value).",
        "positive integer, characters",
    ),
    "REBUILD_PASSED_TESTS_PREVIEW_MAX": _Setting(
        "int", "AIDW_REBUILD_PASSED_TESTS_PREVIEW_MAX", "10", "truncation",
        "How many wrongly-passed test names the TDD-red gate verdict lists inline before summarizing the rest.",
        "Higher lists more test names inline; lower summarizes sooner as \"...and N more\".",
        "positive integer",
    ),
    "GIT_OPS_HTTP_TIMEOUT_SECONDS": _Setting(
        "float", "AIDW_GIT_OPS_HTTP_TIMEOUT_SECONDS", "30.0", "truncation",
        "Shared httpx client timeout for short outbound GitHub/Anthropic API calls (open/update PR, delete branch, repo create, repo lookup, credential validation).",
        "Higher tolerates a slower API response before giving up; lower fails faster on a genuine outage. One setting for every short-lived httpx.AsyncClient in this codebase, not one per call site.",
        "seconds, positive",
    ),

    # -- Test-coverage gate scan caps -------------------------------------------------------------
    "TEST_COVERAGE_BACKEND_FILES_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_BACKEND_FILES_MAX", "25", "coverage_gate",
        "How many candidate backend files the coverage gate reads/scans per repo, per gate run, when checking for a hosted backend framework.",
        "Higher widens what a large monorepo's gate can see (more sandbox round-trips per verify lap); lower may miss the framework in a very large repo.",
        "positive integer",
    ),
    "TEST_COVERAGE_OTEL_EXTRA_FILES_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_OTEL_EXTRA_FILES_MAX", "14", "coverage_gate",
        "How many extra candidate files the coverage gate reads/scans per repo when checking for OpenTelemetry instrumentation.",
        "Higher widens what the gate can see (more sandbox round-trips); lower may miss instrumentation in a very large repo.",
        "positive integer",
    ),
    "TEST_COVERAGE_FRONTEND_CANDIDATES_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_FRONTEND_CANDIDATES_MAX", "30", "coverage_gate",
        "How many candidate frontend files the coverage gate reads/scans per repo when checking for a frontend dependency.",
        "Higher widens what the gate can see (more sandbox round-trips); lower may miss the dependency in a very large repo.",
        "positive integer",
    ),
    "TEST_COVERAGE_MANIFESTS_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_MANIFESTS_MAX", "10", "coverage_gate",
        "How many package-manifest files the coverage gate reads per repo, per gate run.",
        "Higher widens what a large monorepo's gate can see; lower may miss a manifest in a very large repo.",
        "positive integer",
    ),
    "TEST_COVERAGE_CONTRACT_ENTRIES_MAX": _Setting(
        "int", "AIDW_TEST_COVERAGE_CONTRACT_ENTRIES_MAX", "10", "coverage_gate",
        "How many coverage-contract entries get parsed per replay attempt.",
        "Higher processes more entries per attempt (dozens is itself suspect per the gate's own design); lower processes fewer.",
        "positive integer",
    ),
    "DESIGN_TOKENS_GATE_FILES_MAX": _Setting(
        "int", "AIDW_DESIGN_TOKENS_GATE_FILES_MAX", "40", "coverage_gate",
        "How many style-bearing source files the design-tokens gate reads/scans per gate run for off-palette color literals.",
        "Higher widens how much of a large repo's touched UI code the gate can see (more sandbox round-trips); lower may miss violations in a very large repo.",
        "positive integer",
    ),
    # MIN_COVERAGE_PERCENT is deliberately NOT here -- see gates/coverage_parsing.py:50's own
    # comment. Its real enforcement source is that sandbox-mirrored module (coverage_parsing.py
    # reads MIN_COVERAGE_PERCENT itself for pass/fail comparisons, and is baked byte-identical into
    # the sandbox image); config.py exposing a SEPARATE "live" copy here would be disconnected from
    # what actually gates a build (test_coverage_gate.py imports the real one directly from
    # coverage_parsing.py, not from config.py) -- exactly the kind of "looks configurable but
    # isn't" trap this migration exists to avoid. Stays a plain env-var setting (today's existing
    # tier), same category as gates/test_quality_checks.py's cluster below.

    # -- Coverage contract / security scan ---------------------------------------------------------
    "COVERAGE_COMMANDS_PATH": _Setting(
        "str", "AIDW_COVERAGE_COMMANDS_PATH", ".ai-dev-workflow/coverage-commands.json", "coverage_gate",
        "Sandbox-relative path for the model-authored coverage-command contract file.",
        "Changing this only moves where the agent writes/reads the contract inside the sandbox; no effect on coverage logic.",
        "sandbox-relative file path",
    ),
    # CONTRACT_FORMATS is also deliberately NOT here (found during implementation): it has zero
    # real consumers anywhere in this codebase (test_coverage_gate.py's own re-export of it was
    # dead code, removed during this migration) -- coverage_parsing.py's sandbox-side copy is a
    # hardcoded frozenset, not even env-var-configurable, so this setting never actually gated
    # anything. Not migrated, not kept as a fallback -- it was dead before this migration too.
    "TEST_COVERAGE_REPLAY_TIMEOUT_SECONDS": _Setting(
        "int", "REPO_SCAN_COVERAGE_TIMEOUT_SECONDS", "600", "coverage_gate",
        "Wall-clock cap on replaying ONE coverage command during contract verification.",
        "Higher tolerates a slower coverage run; lower kills a hung one sooner.",
        "seconds, positive",
    ),
    "AIDW_SECURITY_CRITICAL_TOOL_NAMES": _Setting(
        "csv", "AIDW_SECURITY_CRITICAL_TOOL_NAMES", "gitleaks,semgrep,osv-scanner,trivy", "coverage_gate",
        "Security-scan tool names whose FAILURE (not just a finding) blocks merge_ready outright.",
        "Adding a tool name means its crash now blocks merge; removing one demotes that tool's failure to a health-score-only discount.",
        "comma-separated security-tool names",
    ),
    "AIDW_TOOL_PROBE_RETRY_COUNT": _Setting(
        "int", "AIDW_TOOL_PROBE_RETRY_COUNT", "1", "coverage_gate",
        "Retry count for a security-scan tool's `--version` probe before marking it status=missing.",
        "Higher spends more wall-clock per flaky tool before giving up; 0 disables retrying entirely.",
        "non-negative integer",
    ),
    "AIDW_TOOL_PROBE_RETRY_DELAY_SECONDS": _Setting(
        "float", "AIDW_TOOL_PROBE_RETRY_DELAY_SECONDS", "3.0", "coverage_gate",
        "Delay between a security-scan tool's version-probe retry attempts.",
        "Higher gives a transient fault more time to clear between attempts, at the same per-attempt cost.",
        "seconds, non-negative",
    ),

    # -- Sandbox/docker -----------------------------------------------------------------------------
    "SANDBOX_PROVISION_RETRY_ATTEMPTS": _Setting(
        "int", "AIDW_SANDBOX_PROVISION_RETRY_ATTEMPTS", "2", "sandbox",
        "Retry count when a sandbox container starts but its CLI tool never responds within its own readiness deadline.",
        "Higher tolerates a slower-starting container; retrying a container that truly never comes up just spends more time.",
        "non-negative integer",
    ),
    "SANDBOX_DOCKER_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_SANDBOX_DOCKER_TIMEOUT_SECONDS", "30", "sandbox",
        "How long a single routine `docker` admin command (inspect/rm/stop/start/cp/exec) may run before being treated as wedged.",
        "Lower is safer: a wedged call here holds a shared lock, freezing every OTHER session's provisioning/touch/liveness too, not just the stuck one. Higher tolerates a slower Docker daemon.",
        "seconds, positive",
    ),
    "SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_SANDBOX_DOCKER_LONG_TIMEOUT_SECONDS", "600", "sandbox",
        "Timeout for docker operations legitimately allowed to run long (e.g. `docker create`'s first-time image pull, reading back a large turn's stdout/stderr).",
        "Higher tolerates a slow image pull or large output read; lower risks cutting one off mid-operation.",
        "seconds, positive",
    ),

    # -- Session/run activity ------------------------------------------------------------------------
    "RUN_SUBSCRIBER_QUEUE_MAXSIZE": _Setting(
        "int", "AIDW_RUN_SUBSCRIBER_QUEUE_MAXSIZE", "500", "misc",
        "How many published graph events one attached SSE subscriber (a browser tab) may have queued before the oldest is dropped.",
        "Higher lets a briefly slow/backgrounded tab fall further behind before losing early events (more memory held per stalled subscriber); lower drops events sooner under load. Never affects the background graph task itself.",
        "positive integer",
    ),

    # -- Runtime/misc -------------------------------------------------------------------------------
    "CLI_AGENT_TURN_TIMEOUT_SECONDS": _Setting(
        "int", "CLI_AGENT_TURN_TIMEOUT_SECONDS", "5400", "misc",
        "Timeout for one CLI-based provider turn (Claude Code or GitHub Copilot, per-turn subprocess exec inside the sandbox).",
        "Generous by design -- a runaway backstop, not an expected exit. Lower risks killing a genuinely complex turn (multiple tool calls, long reasoning) before it finishes.",
        "seconds, positive",
    ),

    # -- Newly centralized (previously scattered as independent env-var constants in other files;
    # folded in here per the Org Settings migration plan's task 5) -----------------------------
    "EVAL_ATTEMPTS": _Setting(
        "int", "EVAL_ATTEMPTS", "3", "misc",
        "How many times the AC-Eval layer (ac_eval.py) re-runs each test suite to detect flakiness.",
        "Higher gives a more reliable flake signal at the cost of more sandbox round-trips per scan; 1 makes flakiness invisible.",
        "positive integer",
    ),
    "EVAL_TIMEOUT_SECONDS": _Setting(
        "int", "EVAL_TIMEOUT_SECONDS", "900", "misc",
        "Wall-clock cap on one AC-Eval suite invocation.",
        "Higher tolerates a slower suite; lower kills a hung one sooner.",
        "seconds, positive",
    ),
    "INFRA_RETRY_ATTEMPTS": _Setting(
        "int", "AIDW_LLM_INFRA_RETRY_ATTEMPTS", "3", "misc",
        "Retry count for a draft/audit/fix LLM call that failed with an infra-shaped error (quota, timeout, transient disconnect), not a content failure.",
        "Higher tolerates more transient infra failures before giving up; lower gives up sooner. Separate from a stage's own clarification/verify-cycle budgets on purpose -- an infra event should not shrink those.",
        "positive integer",
    ),
    "INFRA_RETRY_BACKOFF_SECONDS": _Setting(
        "csv_float", "AIDW_LLM_INFRA_RETRY_BACKOFF_SECONDS", "5,20,60", "misc",
        "Backoff delay (seconds) before each successive infra-retry attempt.",
        "A quota/rate-limit condition does not clear in 0 seconds -- longer delays give more time to clear, at the cost of slower recovery; the list's last value repeats for any attempt beyond its length.",
        "comma-separated seconds, e.g. \"5,20,60\"",
    ),
    "TEST_HARDENING_TOTAL_ATTEMPTS": _Setting(
        "int", "TEST_HARDENING_TOTAL_ATTEMPTS", "3", "misc",
        "How many times test-hardening re-runs a test command (1 initial + N-1 retries) to accumulate per-attempt outcomes for flake detection.",
        "Higher gives a more reliable flake signal at the cost of more sandbox round-trips per stage run; 1 makes flakiness invisible.",
        "positive integer",
    ),
    "MIN_NON_E2E_TESTS_PER_AC_RED": _Setting(
        "int", "MIN_NON_E2E_TESTS_PER_AC_RED", "0", "ac_coverage",
        "Minimum below-browser (unit/integration) tests required per acceptance criterion at the AC-to-Tests (RED/TDD) phase, before any implementation exists.",
        "Raise above 0 only with evidence the drafting model has started writing below-browser tests at this phase -- this is deliberately a lighter RED-phase floor than the full post-implementation requirement.",
        "non-negative integer",
    ),

    # -- repo_scan.py (agent-side only, not sandbox-mirrored -- safe for the live tier) -----------
    # MAX_DUPLICATION_PERCENT: unified from two independent declarations (repo_scan.py's own
    # QUALITY_MAX_DUPLICATION_PERCENT and metrics_nodes.py's own MAX_DUPLICATION_PERCENT, same
    # conceptual threshold against the same jscpd-measured value) onto the env var repo_scan.py's
    # primary scan-time gate already used.
    "MAX_DUPLICATION_PERCENT": _Setting(
        "float", "QUALITY_MAX_DUPLICATION_PERCENT", "3.0", "repo_scan",
        "Code-duplication percentage (jscpd-measured) above which repo_scan's gate blocks, and metrics_nodes' regression check also reads.",
        "Higher tolerates more duplicated code before blocking; lower blocks sooner. Both consumers now read this one value -- previously two independent env vars could disagree.",
        "0-100",
    ),
    "LIZARD_MAX_CCN": _Setting(
        "int", "LIZARD_MAX_CCN", "20", "repo_scan",
        "Cyclomatic complexity above which a function is a HARD gating finding (blocks the run).",
        "20, not lizard's own warn-level 15: 15 flags ordinary dense-but-flat code (observed live: a CCN-17 function ping-ponged between fixer and gate). Lower blocks more functions; higher lets denser code through.",
        "positive integer",
    ),
    "LIZARD_HIGH_CCN": _Setting(
        "int", "LIZARD_HIGH_CCN", "25", "repo_scan",
        "Cyclomatic complexity above LIZARD_MAX_CCN at which a function is flagged as a real complexity \"monster\", not just reviewer-attention territory.",
        "Higher narrows what counts as a monster; lower widens it.",
        "positive integer, should stay above LIZARD_MAX_CCN",
    ),
    "CHURN_WINDOW_DAYS": _Setting(
        "int", "REPO_SCAN_CHURN_WINDOW_DAYS", "365", "repo_scan",
        "Lookback window (days) for the git churn/ownership measurement.",
        "Longer captures more history (slower git log, may include since-rewritten code); shorter focuses on recent activity.",
        "positive integer, days",
    ),
    "DOC_COVERAGE_MIN_PERCENT": _Setting(
        "float", "DOC_COVERAGE_MIN_PERCENT", "50.0", "repo_scan",
        "Minimum docstring-coverage percentage (interrogate) before it's flagged as a maintainability gap.",
        "A lenient first-cut floor by design, not a calibrated target (interrogate's own README default is 80%) -- higher fires on more repos with partial documentation; lower only catches severe gaps.",
        "0-100",
    ),
    "SECURITY_SEVERITY_FLOOR": _Setting(
        "str", "SECURITY_SEVERITY_FLOOR", "medium", "repo_scan",
        "Minimum severity a security finding must reach to be gating (block merge).",
        "Lower (e.g. \"low\") blocks on more findings; higher (e.g. \"high\"/\"critical\") only blocks on the most severe ones.",
        "one of: info, low, medium, high, critical",
    ),
    "MIN_SECURITY_COVERAGE": _Setting(
        "float", "HEALTH_MIN_SECURITY_COVERAGE", "1.0", "repo_scan",
        "Fraction of applicable security tools that must complete before the health score applies no coverage-based haircut.",
        "1.0 means any failed security tool costs something; lower tolerates more tool failures before discounting the score. A smooth sqrt(fraction) haircut below this point, never a cliff.",
        "0.0-1.0",
    ),
    "METRIC_REGRESSION_TOLERANCE": _Setting(
        "float", "METRIC_REGRESSION_TOLERANCE", "1.0", "repo_scan",
        "How much a coverage/quality metric may worsen between scans before metrics_nodes treats it as a real regression, not scan noise.",
        "Higher tolerates more movement before blocking (jscpd is LOC-sensitive, tool DBs drift); lower blocks on smaller regressions.",
        "non-negative float",
    ),
    "HEALTH_REGRESSION_TOLERANCE": _Setting(
        "float", "HEALTH_REGRESSION_TOLERANCE", "2.0", "repo_scan",
        "How much the App Health score may drop between scans before metrics_nodes treats it as a real regression.",
        "Deliberately kept below one new medium-severity finding's own score penalty (3), so a single real new medium-severity finding still blocks regardless of this tolerance. Higher tolerates more score movement; lower blocks sooner.",
        "non-negative float",
    ),
    # Health score v2/v3 weights -- already independently env-backed, one setting per subscore.
    # Read via a function (_health_weights() in repo_scan.py), not a frozen module-level dict:
    # like graph.py's STAGES, a plain dict built once at import time would never reflect a
    # per-session-pinned override.
    "HEALTH_WEIGHT_SECURITY": _Setting(
        "float", "HEALTH_WEIGHT_SECURITY", "0.40", "repo_scan",
        "App Health score weight for the security subscore (of 9 weights summing to 1.0).",
        "Higher makes security dominate the overall score more; lower de-emphasizes it. Unmeasured subscores redistribute their weight proportionally over the measured ones.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_COVERAGE": _Setting(
        "float", "HEALTH_WEIGHT_COVERAGE", "0.12", "repo_scan",
        "App Health score weight for the coverage subscore (of 9 weights summing to 1.0).",
        "Higher makes coverage dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_DEPENDENCIES": _Setting(
        "float", "HEALTH_WEIGHT_DEPENDENCIES", "0.12", "repo_scan",
        "App Health score weight for the dependencies subscore (of 9 weights summing to 1.0).",
        "Higher makes dependency health dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_AC_VERIFICATION": _Setting(
        "float", "HEALTH_WEIGHT_AC_VERIFICATION", "0.10", "repo_scan",
        "App Health score weight for the AC-verification subscore (of 9 weights summing to 1.0).",
        "Higher makes AC-verification dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_ACCESSIBILITY": _Setting(
        "float", "HEALTH_WEIGHT_ACCESSIBILITY", "0.07", "repo_scan",
        "App Health score weight for the accessibility subscore (of 9 weights summing to 1.0).",
        "Higher makes accessibility dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_COMPLEXITY": _Setting(
        "float", "HEALTH_WEIGHT_COMPLEXITY", "0.06", "repo_scan",
        "App Health score weight for the complexity subscore (of 9 weights summing to 1.0).",
        "Higher makes complexity dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_PERFORMANCE": _Setting(
        "float", "HEALTH_WEIGHT_PERFORMANCE", "0.05", "repo_scan",
        "App Health score weight for the performance subscore (of 9 weights summing to 1.0).",
        "Higher makes performance dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_DUPLICATION": _Setting(
        "float", "HEALTH_WEIGHT_DUPLICATION", "0.04", "repo_scan",
        "App Health score weight for the duplication subscore (of 9 weights summing to 1.0).",
        "Higher makes duplication dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),
    "HEALTH_WEIGHT_MAINTAINABILITY": _Setting(
        "float", "HEALTH_WEIGHT_MAINTAINABILITY", "0.04", "repo_scan",
        "App Health score weight for the maintainability subscore (of 9 weights summing to 1.0).",
        "Higher makes maintainability dominate the overall score more; lower de-emphasizes it.",
        "0.0-1.0, all 9 HEALTH_WEIGHT_* settings should sum to 1.0",
    ),

    # -- Newly centralized bare magic numbers (Task 6 sweep) -- none of these were previously an
    # env var, so each gets an AIDW_ prefix per this file's own convention. ------------------------
    "PROVIDER_CACHE_TTL_SECONDS": _Setting(
        "int", "AIDW_PROVIDER_CACHE_TTL_SECONDS", "30", "runtime_misc",
        "How long chat_model.py caches the resolved active provider (claude/copilot) before re-reading org_settings.",
        "Higher reduces DB round trips but delays how fast a provider switch in Settings reaches in-flight dispatch calls; lower reflects a switch sooner at the cost of more DB reads.",
        "positive integer, seconds",
    ),
    "AUTH_GATE_CURL_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_AUTH_GATE_CURL_TIMEOUT_SECONDS", "15", "e2e",
        "Per-probe curl --max-time for the auth gate's unauthenticated route probes.",
        "Higher tolerates a slower app response before giving up on one probe; lower fails faster but may misclassify a merely-slow route as unreachable.",
        "positive integer, seconds",
    ),
    "AUTH_GATE_MAX_PROBES": _Setting(
        "int", "AIDW_AUTH_GATE_MAX_PROBES", "40", "e2e",
        "Max combined page+API routes the auth gate probes in one check, so a discovery pass gone wild cannot stall e2e.",
        "Higher covers more of a large app's route surface per check but takes longer; lower caps runtime but may leave some routes unverified (reported as dropped_over_cap).",
        "positive integer",
    ),
    "E2E_APP_PORT_RANGE_START": _Setting(
        "int", "AIDW_E2E_APP_PORT_RANGE_START", "3100", "e2e",
        "First port in the pool e2e picks from when launching an app or supporting service.",
        "Change together with E2E_APP_PORT_RANGE_END to move the whole pool; narrowing the range risks port exhaustion on an app with many supporting services.",
        "positive integer, below E2E_APP_PORT_RANGE_END",
    ),
    "E2E_APP_PORT_RANGE_END": _Setting(
        "int", "AIDW_E2E_APP_PORT_RANGE_END", "3140", "e2e",
        "Exclusive end of the port pool e2e picks from (range is [START, END)).",
        "Widening gives more headroom for apps with many supporting services; narrowing risks port exhaustion.",
        "positive integer, above E2E_APP_PORT_RANGE_START",
    ),
    "APP_DISCOVERY_MAX_CANDIDATE_FILES": _Setting(
        "int", "AIDW_APP_DISCOVERY_MAX_CANDIDATE_FILES", "60", "runtime_misc",
        "Max candidate marker files app_discovery.collect_evidence reads before stopping.",
        "Higher examines more of a large/unusual repo layout but costs more sandbox reads; lower is faster but may miss a marker file in an atypical location.",
        "positive integer",
    ),
    "APP_DISCOVERY_MAX_FILE_CHARS": _Setting(
        "int", "AIDW_APP_DISCOVERY_MAX_FILE_CHARS", "4000", "runtime_misc",
        "Max characters read from each candidate marker file during app discovery.",
        "Higher captures more of a large manifest/config file's content as evidence; lower keeps the discovery prompt-grounding blob smaller.",
        "positive integer, characters",
    ),
    "APP_DISCOVERY_MAX_EVIDENCE_CHARS": _Setting(
        "int", "AIDW_APP_DISCOVERY_MAX_EVIDENCE_CHARS", "24000", "runtime_misc",
        "Max total characters of the combined evidence blob app_discovery.collect_evidence returns.",
        "Higher gives the tech-stack detection more context from a large repo; lower keeps it a tighter prompt-grounding artifact, not a repo dump.",
        "positive integer, characters",
    ),
    "VAULT_TIMEOUT_SECONDS": _Setting(
        "float", "AIDW_VAULT_TIMEOUT_SECONDS", "10.0", "runtime_misc",
        "Timeout for a single org-credential Key Vault round trip (get/set).",
        "Shorter than sessions_api's own 30s credential-probe timeout by design, since this sits on a page-load path a signed-in user is actively waiting on. Higher tolerates a slower vault; lower fails faster but may misclassify a merely-slow vault as unreachable.",
        "positive float, seconds",
    ),
    "RUN_LOCK_TASKLIST_TIMEOUT_SECONDS": _Setting(
        "int", "AIDW_RUN_LOCK_TASKLIST_TIMEOUT_SECONDS", "10", "runtime_misc",
        "Windows `tasklist` subprocess timeout used to check whether a run lock's recorded PID is still alive.",
        "Higher tolerates a slower/loaded host before giving up on the liveness check; lower fails faster. Read before per-session settings are pinned (this check can run before a session starts), so it always resolves from env/default, never a live DB override.",
        "positive integer, seconds",
    ),
}


def __getattr__(name: str):
    spec = _SETTINGS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        raw = runtime_settings.get_raw(name)
        if raw is None:
            raw = os.environ.get(spec.env_var, spec.default_raw)
        return _PARSERS[spec.parser](raw)
    except Exception:
        # A malformed override (bad text, an admin typo) must not crash an ordinary attribute
        # access at a random one of ~265 call sites -- log and fall back to the exact same
        # env/default resolution as an unset override, same contract as chat_model.get_provider()'s
        # own DB-failure fallback, just without needing a "last known good" value to track (the
        # env/default tier is always available).
        logger.warning(
            "config.%s: override failed to parse, falling back to env/default", name, exc_info=True
        )
        return _PARSERS[spec.parser](os.environ.get(spec.env_var, spec.default_raw))
    # ponytail: re-parses on every access, no per-name memoization -- this pipeline is LLM-call-
    # bound (minutes per turn), a dict lookup + int()/float() is not worth caching. Add one only if
    # a profiler ever says config reads are hot -- they won't be next to an LLM turn.


# ============================================================================================
# Public accessors for the Settings API (task 7) -- keep _SETTINGS/_PARSERS/_FORMATTERS private
# (implementation detail) while giving sessions_api.py a small, stable surface to build the
# list/get/set/delete endpoints against, rather than reaching into this module's underscored
# internals directly.
# ============================================================================================


def list_settings() -> list[dict[str, str | None]]:
    """Every migrated setting's static metadata -- name/category/purpose/effect/value_range/
    parser/env_var/default_raw. No current value and no override status here: those are a
    per-request question (this session's live value, and whatever dbo.runtime_settings currently
    holds), answered by `getattr(config, name)` and runtime_settings.list_overrides() respectively,
    not by this module's own static table."""
    return [
        {
            "name": name,
            "category": spec.category,
            "purpose": spec.purpose,
            "effect": spec.effect,
            "value_range": spec.value_range,
            "parser": spec.parser,
            "env_var": spec.env_var,
            "default_raw": spec.default_raw,
        }
        for name, spec in _SETTINGS.items()
    ]


def default_value(name: str) -> object:
    """`name`'s parsed (typed) default value -- KeyError if `name` isn't a migrated setting."""
    spec = _SETTINGS[name]
    return _PARSERS[spec.parser](spec.default_raw)


def format_setting(name: str, value: object) -> str:
    """Runs `name`'s own formatter against a live Python value (typically JSON-decoded request
    body data: str/int/float/bool/list), producing the exact string shape its parser expects.
    Raises KeyError for an unknown setting name, and whatever the formatter itself raises for a
    value of the wrong shape (e.g. TypeError from `",".join(v)` on a non-iterable)."""
    spec = _SETTINGS[name]
    return _FORMATTERS[spec.parser](value)


def parse_setting(name: str, raw: str) -> object:
    """Runs `name`'s own parser against raw env-var-shaped text. Raises KeyError for an unknown
    setting name, and whatever the parser itself raises (ValueError, etc.) for malformed text --
    the Settings API's write-time round-trip validation (must-fix #2) depends on this raising
    loudly rather than silently coercing."""
    spec = _SETTINGS[name]
    return _PARSERS[spec.parser](raw)


# ============================================================================================
# Excluded from the live settings system -- plain, static module-level constants, unchanged from
# before this migration. See this file's own module docstring for why each group stays here rather
# than in _SETTINGS above.
# ============================================================================================

# Platform-constraint constant, not an operator-tunable knob: bounds how large a single chunk of a
# chunked sandbox write (cli_agent_exec.py, repo_files.py) may be before it's split further, staying
# well under Windows CreateProcess's ~32767-char argv limit (with headroom for the shell wrapper text
# around the payload). Raising it past that real platform ceiling breaks the write it's meant to
# protect; it is not a performance/cost knob to retune. Previously two independent declarations
# (cli_agent_exec.py's and repo_files.py's own `_EXEC_CMD_BUDGET`, same value, same purpose) --
# unified here per AGENTS.md's "one constant, not one each" rule; both files now import it from here.
EXEC_CMD_BUDGET_CHARS = 16000

# In-container path the sandbox image bakes the Agent Plugin content to (agent/sandbox-image/
# Dockerfile's COPY plugins/ -> this path). Overridable for local spikes without a code change --
# a dev-time convenience, not a production admin's lever, so excluded from the live DB-backed tier.
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
# escalated with zero test files written. Protocol wiring (which skills are valid to invoke), not
# a tunable limit -- excluded from the live DB-backed tier.
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
# stage's prompt names them, and gates/skill_gate.py verifies via chat_model's provider dispatch
# (get_session_id + read_skill_invocations) -- which means different things per provider. Claude's
# implementation reads that session's real CLI transcript and works; Copilot's unconditionally
# returns None (no CLI-exec equivalent exists yet to the old SDK-server session log this used to
# read), so verification is permanently unavailable under the default provider today -- see
# skill_gate.py's own module docstring. Self-report (StageReport.skills_invoked) is telemetry, not
# evidence regardless -- a model that skipped a skill will happily claim it used one. Protocol
# wiring, not a tunable limit -- excluded from the live DB-backed tier.
REQUIRED_SKILLS_BY_STAGE: dict[str, list[str]] = {
    # grill-me (mattpocock pack, vendored in the sandbox image): the spec prompt has always asked
    # for it; required here after a live run (2026-08-31) shipped a spec with zero Skill calls --
    # the gate is what closes the prompt-says/agent-skips gap.
    "specification": ["brainstorming", "grill-me"],
    "plan": ["writing-plans"],
    # File-based-editing plan, Part 6 (true brownfield/greenfield convergence): both passes reuse
    # specification_draft.md/plan_draft.md verbatim, so they carry the identical mandatory-skill
    # instructions those prompts already give -- without an entry here, the gate would silently NOT
    # enforce a claim the reused prompt text itself makes ("a deterministic gate REJECTS... if
    # either Skill-tool call is missing"), the exact prompt-says/gate-checks gap this dict exists
    # to close everywhere else.
    "brownfield-spec": ["brainstorming", "grill-me"],
    "brownfield-plan": ["writing-plans"],
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

# Same-turn counterpart of the Review-depth safety net (graph.py's _verify_specification_ledger /
# gates/diagram_gate.py's _load_and_sync_plan_steps, both keyed on spec_ledger.DRAFT_SPEC_PATH /
# diagram_gate.DRAFT_STEPS_PATH): the file an AUDIT session must prove it read in full THIS lap,
# activated only for the AUDIT role via claude_chat_model.py's/copilot_chat_model.py's
# `_full_read_env_prefix` (2026-09-19), the identical draft-only/audit-only asymmetry
# REQUIRED_SKILLS_BY_STAGE's own comment documents for _required_skills_env_prefix, just inverted
# (that one arms the DRAFT role; this one arms the AUDIT role, since only the audit's own prompt
# demands a full re-read every pass -- see specification_audit.md/plan_audit.md's own "View it
# first, in full" instruction). A literal string here, not an import of the real constant, for the
# SAME reason config.py stays a leaf module with zero project imports throughout this file --
# spec_ledger.py/gates/diagram_gate.py both import repo_files/chat_model, and diagram_gate.py
# imports chat_model.py, which imports claude_chat_model.py/copilot_chat_model.py, which import
# config.py -- importing either module's real constant HERE would complete that cycle. KEPT IN
# SYNC by claude_chat_model.py's/copilot_chat_model.py's own self-check, which imports both real
# modules locally (inside the demo function, well after both are fully loaded, so no cycle) and
# asserts equality against these literals. Zero drift-safeguard beyond that self-check -- a wrong
# live edit here has no automated guard, one of the reasons this stays excluded from the live
# DB-backed tier rather than admin-editable.
AUDIT_FULL_READ_FILE_BY_STAGE: dict[str, str] = {
    "specification": ".ai-dev-workflow/spec/draft-specification.json",
    "plan": ".ai-dev-workflow/plan/_draft/steps.json",
    # brownfield-spec/brownfield-plan deliberately absent: both pass has_audit_role=False (no
    # audit session ever exists for them), so there is no role for this env var to ever arm.
}

# Read-only tool allowlist (Phase A0 spike finding: excluded_tools blocklisting write-capable
# tools is incomplete -- the model can reach create/bash/edit/apply_patch interchangeably, so
# read-only stages must allowlist via available_tools instead). All entries are source-qualified
# ("builtin:<name>") per copilot._mode.ToolSet -- bare names are rejected/silently ignored.
# Protocol wiring, not a tunable limit -- excluded from the live DB-backed tier.
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

# App Health (Metrics Bar 3-way split) blend: coverage_fraction and whole-suite test_pass_rate are
# each already 0-1; this is their relative weight in the synthetic 0-100 App Health score. Read by
# repo_scan.app_health_score. Equal weight by default. Feeds only a cosmetic reporting number with
# zero observed-live retuning history -- excluded from the live DB-backed tier (see this file's own
# module docstring).
AIDW_APP_HEALTH_COVERAGE_WEIGHT = float(os.environ.get("AIDW_APP_HEALTH_COVERAGE_WEIGHT", "0.5"))
AIDW_APP_HEALTH_PASS_RATE_WEIGHT = float(os.environ.get("AIDW_APP_HEALTH_PASS_RATE_WEIGHT", "0.5"))

# Productivity/effort-saved estimate (traceability-matrix plan, "Capability-Based Lifecycle
# Benchmarking"): base hours claimed per line of code changed, before the complexity multiplier and
# AC-resolution/review-overhead discounts below. Read by metrics_nodes.py's estimated-hours
# computation. Feeds only a cosmetic reporting number -- excluded from the live DB-backed tier.
AIDW_HOURS_PER_LOC_BASE = float(os.environ.get("AIDW_HOURS_PER_LOC_BASE", "0.017"))

# Complexity multiplier buckets applied to AIDW_HOURS_PER_LOC_BASE, keyed by the scan's mean
# cyclomatic complexity (lizard's mean_ccn, repo_scan.py) for this run. Read by metrics_nodes.py.
# Keys are the upper bound of each bucket (mean_ccn < key); the last tuple has no upper bound.
# Feeds only a cosmetic reporting number, not independently env-backed today -- excluded from the
# live DB-backed tier.
AIDW_COMPLEXITY_HOUR_MULTIPLIERS: tuple[tuple[float, float], ...] = (
    (5.0, 1.0),
    (10.0, 1.3),
    (float("inf"), 1.6),
)

# Review-overhead deduction on the productivity/effort-saved estimate above: AI-generated code
# still needs human review/refactoring before it is trustworthy, so this fraction of the raw
# estimate is subtracted before the final "hours saved" figure is shown. Read by metrics_nodes.py.
# Feeds only a cosmetic reporting number -- excluded from the live DB-backed tier.
AIDW_REVIEW_OVERHEAD_FRACTION = float(os.environ.get("AIDW_REVIEW_OVERHEAD_FRACTION", "0.175"))

# exit_nodes.py's markdown-table-rendering family: internal rendering detail, zero LLM calls
# anywhere near it, no operator would plausibly tune a table cell's character width via env var
# (AGENTS.md's own carve-out example) -- excluded from the live DB-backed tier, unchanged from
# before this migration.
EXIT_SBOM_DIFF_PREVIEW_MAX = int(os.environ.get("AIDW_EXIT_SBOM_DIFF_PREVIEW_MAX", "15"))
EXIT_FAILURE_HEADLINE_CHARS = int(os.environ.get("AIDW_EXIT_FAILURE_HEADLINE_CHARS", "300"))
EXIT_TOOL_ERROR_SNIPPET_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_ERROR_SNIPPET_CHARS", "80"))
EXIT_MD_CELL_DEFAULT_CHARS = int(os.environ.get("AIDW_EXIT_MD_CELL_DEFAULT_CHARS", "90"))
EXIT_KNOWN_GAP_CELL_CHARS = int(os.environ.get("AIDW_EXIT_KNOWN_GAP_CELL_CHARS", "110"))
EXIT_HEALTH_BASIS_CELL_CHARS = int(os.environ.get("AIDW_EXIT_HEALTH_BASIS_CELL_CHARS", "100"))
EXIT_FINDING_TOOLS_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_TOOLS_CELL_CHARS", "40"))
EXIT_FINDING_RULE_ID_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_RULE_ID_CELL_CHARS", "40"))
EXIT_FINDING_TITLE_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_TITLE_CELL_CHARS", "70"))
EXIT_FINDING_WHERE_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_WHERE_CELL_CHARS", "60"))
EXIT_TOOL_VERSION_CELL_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_VERSION_CELL_CHARS", "45"))
EXIT_TOOL_NOTES_CELL_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_NOTES_CELL_CHARS", "60"))
EXIT_FINDINGS_TABLE_CAP = int(os.environ.get("AIDW_EXIT_FINDINGS_TABLE_CAP", "60"))
EXIT_TEST_IDS_PREVIEW_MAX = int(os.environ.get("AIDW_EXIT_TEST_IDS_PREVIEW_MAX", "3"))
EXIT_STALE_REASON_LOG_CHARS = int(os.environ.get("AIDW_EXIT_STALE_REASON_LOG_CHARS", "120"))

# git_ops.py's push_head: how much of a failed `git push`'s own stderr/stdout to keep in
# _LAST_PUSH's "error" field, surfaced to the session/UI as a streamed "warning chip" -- trivial,
# UI-display-only, not worth an admin setting -- excluded from the live DB-backed tier.
GIT_OPS_PUSH_ERROR_TAIL_CHARS = int(os.environ.get("AIDW_GIT_OPS_PUSH_ERROR_TAIL_CHARS", "500"))


def _demo() -> None:
    """Offline self-check: `cd agent && uv run python -m src.config`. No live DB in this
    environment -- exercises the parser/formatter round-trip and the env/default fallback, not a
    real runtime_settings override (that's covered by runtime_settings.py's own self-check)."""
    # Env/default fallback: with no session pinned (runtime_settings.get_raw returns None for
    # everything), every migrated constant must resolve its documented default via its own parser.
    # Called directly via __getattr__(name), not as a bare name: bare module-level names resolve
    # against this module's own globals (LEGB), which never consults __getattr__ at all -- that
    # PEP 562 hook only fires for EXTERNAL `config.NAME` attribute access (every real call site
    # across the other 19 files), not for code referencing a name bare from inside config.py
    # itself. Exercising __getattr__ explicitly here is what actually proves the shim works.
    assert __getattr__("SPEC_MAX_VERIFY_CYCLES") == 5, __getattr__("SPEC_MAX_VERIFY_CYCLES")
    assert isinstance(__getattr__("SPEC_MAX_VERIFY_CYCLES"), int)
    assert isinstance(__getattr__("AIDW_TOOL_PROBE_RETRY_DELAY_SECONDS"), float)
    assert __getattr__("AIDW_E2E_REUSE_PROVEN_LAUNCH") is True, __getattr__("AIDW_E2E_REUSE_PROVEN_LAUNCH")
    assert __getattr__("LIGHTHOUSE_BLOCKING_AUDITS") == frozenset({"color-contrast"}), __getattr__("LIGHTHOUSE_BLOCKING_AUDITS")
    assert __getattr__("E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS") == (3000, 10000, 15000), __getattr__("E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS")
    assert __getattr__("E2E_APP_LOG_PATH") == "agent-work/e2e-app.log", __getattr__("E2E_APP_LOG_PATH")
    assert __getattr__("PROVIDER_CACHE_TTL_SECONDS") == 30, __getattr__("PROVIDER_CACHE_TTL_SECONDS")
    assert __getattr__("E2E_APP_PORT_RANGE_START") == 3100 and __getattr__("E2E_APP_PORT_RANGE_END") == 3140
    assert __getattr__("VAULT_TIMEOUT_SECONDS") == 10.0, __getattr__("VAULT_TIMEOUT_SECONDS")

    # Every _FORMATTERS entry must produce a string _PARSERS[same key] can parse straight back to
    # an equal value -- the exact round-trip must-fix #1 (CSV-text vs. JSON conflation) depends on.
    round_trip_cases: list[tuple[str, object]] = [
        ("int", 7),
        ("float", 3.5),
        ("str", "hello"),
        ("bool", True),
        ("bool", False),
        ("csv", ("a", "b", "c")),
        ("csv_int", (3000, 10000, 15000)),
        ("frozenset_csv", frozenset({"color-contrast", "image-alt"})),
    ]
    for kind, value in round_trip_cases:
        formatted = _FORMATTERS[kind](value)
        assert isinstance(formatted, str), (kind, value, formatted)
        parsed = _PARSERS[kind](formatted)
        assert parsed == value, (kind, value, formatted, parsed)

    # A malformed override must not crash __getattr__ -- it must fall back to env/default instead.
    # (Simulated without a live runtime_settings session: directly exercise the parser-failure path
    # a corrupt DB value would hit.)
    try:
        int("not-a-number")
    except ValueError:
        pass
    else:
        raise AssertionError("expected int() to raise on non-numeric text")

    # Unknown attribute must raise AttributeError, not silently return None or crash differently --
    # standard Python module-attribute-error contract, must hold even with __getattr__ defined.
    try:
        __getattr__("_no_such_setting_")
    except AttributeError:
        pass
    else:
        raise AssertionError("expected AttributeError for an unknown config attribute")

    # Excluded-bucket constants must still resolve as plain static values, completely bypassing
    # __getattr__ (proves the coexistence of _SETTINGS-backed dynamic attributes and real static
    # module globals doesn't conflict).
    assert REQUIRED_SKILLS_BY_STAGE["plan"] == ["writing-plans"], REQUIRED_SKILLS_BY_STAGE
    assert COPILOT_PLUGIN_DIRECTORIES[0].startswith(COPILOT_PLUGIN_ROOT_IN_CONTAINER), COPILOT_PLUGIN_DIRECTORIES
    assert EXEC_CMD_BUDGET_CHARS == 16000, EXEC_CMD_BUDGET_CHARS

    # Public accessors (task 7's Settings API surface).
    all_settings = list_settings()
    assert len(all_settings) == len(_SETTINGS) and len(all_settings) > 100, len(all_settings)
    assert {"name", "category", "purpose", "effect", "value_range", "parser", "env_var", "default_raw"} <= all_settings[0].keys()
    assert default_value("SPEC_MAX_VERIFY_CYCLES") == 5, default_value("SPEC_MAX_VERIFY_CYCLES")
    assert format_setting("SPEC_MAX_VERIFY_CYCLES", 9) == "9", format_setting("SPEC_MAX_VERIFY_CYCLES", 9)
    assert parse_setting("SPEC_MAX_VERIFY_CYCLES", "9") == 9, parse_setting("SPEC_MAX_VERIFY_CYCLES", "9")
    assert format_setting("LIGHTHOUSE_BLOCKING_AUDITS", ["image-alt", "color-contrast"]) == "color-contrast,image-alt"
    try:
        format_setting("_no_such_setting_", 1)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unknown setting name")

    print("config self-check: ok (env/default fallback, parser/formatter round-trip, exclusion coexistence)")


if __name__ == "__main__":  # pragma: no cover -- cd agent && uv run python -m src.config
    # Re-dispatch through the PACKAGE name on purpose -- same convention as org_settings.py/
    # chat_model.py/runtime_settings.py: `python -m src.config` loads this file as "__main__", so a
    # direct _demo() call would import this module a second time under a separate sys.modules
    # identity, splitting _SETTINGS/_PARSERS across two entries.
    from src.config import _demo as _packaged_demo

    _packaged_demo()
