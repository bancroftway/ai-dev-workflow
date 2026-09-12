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

# Root-caused 2026-09-12: caps how many times POST /api/sessions/actions {action: "targeted-fix"}
# may run its seeded fix pass against one already-closed session (graph.py's intake_node,
# GraphState.targeted_fix_attempts). Unlike rewind-to-stage, this action never resets a stage, so
# nothing else bounds how many times a user could invoke it against the same run -- read by
# intake_node and used by sessions_api.py's rewind endpoint to refuse once exhausted.
TARGETED_FIX_MAX_ATTEMPTS = int(os.environ.get("TARGETED_FIX_MAX_ATTEMPTS", "3"))

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

# rebuild.py's build-output capture for its fix loop (_replay_build's per-command cap, and the
# combined cap applied both there and again when rebuild_node stores rb["last_stdout_tail"]/
# last_stderr_tail -- the two fields the fix prompt actually reads). Observed live (2026-09-09,
# greenfield angular-dotnet): a 126-error `dotnet build` with a 2000/4000-char cap left the fix
# model seeing only its last ~10 errors every lap, so 3 fix cycles never converged -- the errors
# outside the tail were structurally invisible, not merely deprioritized. Widened, not removed:
# still bounded so a truly pathological log can't blow up prompt size/cost unbounded.
REBUILD_OUTPUT_TAIL_CHARS = int(os.environ.get("AIDW_REBUILD_OUTPUT_TAIL_CHARS", "8000"))
REBUILD_OUTPUT_COMBINED_TAIL_CHARS = int(os.environ.get("AIDW_REBUILD_OUTPUT_COMBINED_TAIL_CHARS", "16000"))

# The same tail-only-truncation bug the pair above fixes for rebuild.py, found live in 4 more
# spots by the follow-up audit that produced this comment. Each pair below uses
# text_truncate.truncate_middle to keep BOTH ends of the captured text instead of just the tail,
# so effect-of-change is identical in shape everywhere: raising either HEAD or TAIL widens how
# much of that end survives; the total (HEAD+TAIL) is the point below which nothing is cut at all.

# gates/diagram_gate.py's _render_one: mmdc's own stdout/stderr for one diagram render. Read by
# the draft node's next redraft as feedback. Was already fixed correctly (this is the reference
# pattern) but hardcoded 2000/2000 (4000 total) -- relocated here, values unchanged.
DIAGRAM_ERROR_SUMMARY_HEAD_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_HEAD_CHARS", "2000"))
DIAGRAM_ERROR_SUMMARY_TAIL_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_TAIL_CHARS", "2000"))

# gates/diagram_gate.py's plan-diagram caps: how many diagrams a plan may include, and how large
# one wireframe's HTML may be, before the deterministic_verify gate rejects the draft outright and
# asks for fewer/smaller ones. Purely a plan-content ceiling, unrelated to the truncation pairs
# above -- relocated from local module constants of the same name, values unchanged.
DIAGRAM_MAX_WIREFRAMES = int(os.environ.get("AIDW_DIAGRAM_MAX_WIREFRAMES", "6"))
DIAGRAM_MAX_WIREFRAME_BYTES = int(os.environ.get("AIDW_DIAGRAM_MAX_WIREFRAME_BYTES", str(30 * 1024)))

# gates/test_coverage_gate.py's _replay_coverage_contract: one coverage-command's raw stdout/
# stderr, captured before it's joined into failure_detail below. Read by _run_coverage_via_ghcp's
# next discovery attempt. 750/750 (1500 total) matches the pre-existing tail-only budget; shape
# fixed, size unchanged -- unconfirmed whether this one has bitten a real run yet (unlike
# rebuild.py's), so no widen without an observed incident.
TEST_COVERAGE_OUTPUT_HEAD_CHARS = int(os.environ.get("AIDW_TEST_COVERAGE_OUTPUT_HEAD_CHARS", "750"))
TEST_COVERAGE_OUTPUT_TAIL_CHARS = int(os.environ.get("AIDW_TEST_COVERAGE_OUTPUT_TAIL_CHARS", "750"))

# gates/test_coverage_gate.py's _run_coverage_via_ghcp: the one-line-per-command failure_detail
# summary joined from the (already-capped) tails above -- this used to re-truncate that value a
# SECOND time, tail-only, the exact "truncated twice" shape rebuild.py had. 150/150 (300 total)
# matches the pre-existing budget for this per-entry summary line.
TEST_COVERAGE_FAILURE_DETAIL_HEAD_CHARS = int(os.environ.get("AIDW_TEST_COVERAGE_FAILURE_DETAIL_HEAD_CHARS", "150"))
TEST_COVERAGE_FAILURE_DETAIL_TAIL_CHARS = int(os.environ.get("AIDW_TEST_COVERAGE_FAILURE_DETAIL_TAIL_CHARS", "150"))

# exit_nodes.py's _render_terminal_failure: the "## Terminal failure" code block in the final exit
# report -- the human-facing summary of why a run died. Its `detail` can be graph.py's verify-cap
# `feedback`, which graph.py's own comments note can be an adversarial-compliance LIST of
# findings; tail-only here silently dropped the first ones. 1250/1250 (2500 total, unchanged).
EXIT_FAILURE_DETAIL_HEAD_CHARS = int(os.environ.get("AIDW_EXIT_FAILURE_DETAIL_HEAD_CHARS", "1250"))
EXIT_FAILURE_DETAIL_TAIL_CHARS = int(os.environ.get("AIDW_EXIT_FAILURE_DETAIL_TAIL_CHARS", "1250"))

# e2e_nodes.py's readiness-failure description text (deferred-service retry AND main-app boot,
# both embed this in e2e["failed_tests"][...]["error"]) -- not the fix prompt itself, see the pair
# below for that. 1500/1500 (3000 total, unchanged).
E2E_BOOT_FAILURE_LOG_HEAD_CHARS = int(os.environ.get("AIDW_E2E_BOOT_FAILURE_LOG_HEAD_CHARS", "1500"))
E2E_BOOT_FAILURE_LOG_TAIL_CHARS = int(os.environ.get("AIDW_E2E_BOOT_FAILURE_LOG_TAIL_CHARS", "1500"))

# e2e_nodes.py's e2e_fix_node: the app log tail handed straight to E2E_FIX_HUMAN_TEMPLATE as the
# fix model's own prompt input -- the actual fix-loop-facing field, kept separate from the
# boot-failure-description pair above so this budget can be tuned independently (same reasoning
# as rebuild.py treating its fix-prompt fields separately from other tails). 2000/2000 (4000
# total, unchanged).
E2E_FIX_APP_LOG_HEAD_CHARS = int(os.environ.get("AIDW_E2E_FIX_APP_LOG_HEAD_CHARS", "2000"))
E2E_FIX_APP_LOG_TAIL_CHARS = int(os.environ.get("AIDW_E2E_FIX_APP_LOG_TAIL_CHARS", "2000"))

# rebuild.py's make_rebuild_node: the DURABLE ledger record of why the TDD-red gate blocked a
# build. 1500, not a smaller head-only preview: the detail is a LIST of tests that wrongly passed,
# and a 300-char cap once stopped inside the first entry -- the ledger recorded that the gate
# fired without recording what it found. Head-only (not head+tail) is correct here: the list's
# start is what a human reviewing the ledger needs, unlike the fix-loop tails above which need
# both ends because the model reads the whole thing back.
REBUILD_LEDGER_DETAIL_CHARS = int(os.environ.get("AIDW_REBUILD_LEDGER_DETAIL_CHARS", "1500"))

# rebuild.py's make_escalate_node: the one-line `feedback` field session_store._build_failure reads
# for the DB row's failure_message, once the rebuild fix-cycle cap is already exhausted (this runs
# only after fixing is over, so it doesn't feed another fix attempt the way the pairs above do).
REBUILD_ESCALATE_FEEDBACK_CHARS = int(os.environ.get("AIDW_REBUILD_ESCALATE_FEEDBACK_CHARS", "1000"))

# rebuild.py's scan-delta gate reason list (the "blocking on N reason(s)" log line fed to
# _detect_verify_stall/warning output, not a fix prompt): how many gating findings to list before
# summarizing the rest as "...and N more", and how much of one finding's own title/message to show
# per line.
REBUILD_GATING_FINDINGS_MAX = int(os.environ.get("AIDW_REBUILD_GATING_FINDINGS_MAX", "10"))
REBUILD_FINDING_MESSAGE_CHARS = int(os.environ.get("AIDW_REBUILD_FINDING_MESSAGE_CHARS", "110"))

# rebuild.py's TDD-red gate verdict messages (ticket-scope and whole-suite variants): how many
# wrongly-passed test names to list inline before summarizing the rest as "...and N more".
REBUILD_PASSED_TESTS_PREVIEW_MAX = int(os.environ.get("AIDW_REBUILD_PASSED_TESTS_PREVIEW_MAX", "10"))

# e2e_nodes.py's sandbox-relative scratch paths for the booted app under test: where its stdout+
# stderr are redirected (LOG) and where its PID is recorded so it can be killed after the suite
# runs (PID). Changing either just moves where the agent writes/reads inside the sandbox -- no
# effect on suite behavior, but LOG_PATH must stay in sync with the shell redirect that creates it
# (_boot_process) and PID_PATH with the shell snippet that kills it.
E2E_APP_LOG_PATH = os.environ.get("AIDW_E2E_APP_LOG_PATH", "agent-work/e2e-app.log")
E2E_APP_PID_PATH = os.environ.get("AIDW_E2E_APP_PID_PATH", "agent-work/e2e-app.pid")

# e2e_nodes.py's _probe_page: when the page-probe script's stdout isn't parseable JSON, how much of
# the raw output to fold into the diagnostic error string (a probe failure, not test output --
# never fed to a model, just surfaced in failed_tests for a human/the fix loop's context).
E2E_PROBE_PREVIEW_CHARS = int(os.environ.get("AIDW_E2E_PROBE_PREVIEW_CHARS", "300"))
# e2e_nodes.py's summarise_page_state: how many captured browser console errors to list, and how
# much of the page's rendered text to show, per probed route.
E2E_CONSOLE_ERRORS_MAX = int(os.environ.get("AIDW_E2E_CONSOLE_ERRORS_MAX", "5"))
E2E_PAGE_TEXT_PREVIEW_CHARS = int(os.environ.get("AIDW_E2E_PAGE_TEXT_PREVIEW_CHARS", "600"))

# e2e_nodes.py's degenerate_screenshots: a PNG at or below this size is treated as evidence the
# page painted nothing (observed live: five blank captures were all exactly 4254 bytes). Raising
# it risks flagging a genuinely tiny-but-real page as blank; lowering it risks missing a blank
# capture that happens to be a few bytes larger.
E2E_DEGENERATE_PNG_MAX_BYTES = int(os.environ.get("AIDW_E2E_DEGENERATE_PNG_MAX_BYTES", "8192"))
# e2e_nodes.py's per-route screenshot retry ladder (milliseconds between capture attempts) for a
# client-rendered app that hasn't hydrated yet -- see the ladder's own long comment at its call
# site for the Blazor measurement this was tuned against. Escalating, not fixed, because hydration
# time varies by stack; total worst case per route is the ladder's sum, bounded by
# E2E_ROUTES_MAX captures.
E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS = tuple(
    int(ms) for ms in os.environ.get("AIDW_E2E_ROUTE_SCREENSHOT_HYDRATE_LADDER_MS", "3000,10000,15000").split(",")
)
# e2e_nodes.py's screenshot/lighthouse harvest loops (3 call sites): how many routes to capture
# per run. Raising this linearly increases both wall-clock time (each route pays the full hydrate
# ladder above on a cold render) and the number of screenshots in the exit report.
E2E_ROUTES_MAX = int(os.environ.get("AIDW_E2E_ROUTES_MAX", "12"))

# e2e_nodes.py's per-route screenshot/lighthouse failure log lines (logger.warning only, never
# reach a model) -- how much of that one command's own stdout to include in the log message.
E2E_SCREENSHOT_STDOUT_TAIL_CHARS = int(os.environ.get("AIDW_E2E_SCREENSHOT_STDOUT_TAIL_CHARS", "500"))
E2E_LIGHTHOUSE_STDOUT_TAIL_CHARS = int(os.environ.get("AIDW_E2E_LIGHTHOUSE_STDOUT_TAIL_CHARS", "300"))
# e2e_nodes.py's _run_lighthouse: hard wall-clock cap (via `timeout`) on one route's lighthouse
# run. A route that hangs past this is skipped (fail-open, never scored as 0) rather than wedging
# the whole e2e stage.
E2E_LIGHTHOUSE_TIMEOUT_SECONDS = int(os.environ.get("AIDW_E2E_LIGHTHOUSE_TIMEOUT_SECONDS", "150"))

# gates/test_coverage_gate.py's missing_hosted_backend/missing_frontend_dependency source-reading
# helpers: how many candidate files of each kind to actually read_repo_file and scan, per repo, per
# gate run. Raising these widens what a large monorepo's coverage gate can see before giving up and
# assuming the framework is missing, at the cost of more sandbox round-trips per verify lap.
TEST_COVERAGE_BACKEND_FILES_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_BACKEND_FILES_MAX", "25"))
TEST_COVERAGE_OTEL_EXTRA_FILES_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_OTEL_EXTRA_FILES_MAX", "14"))
TEST_COVERAGE_FRONTEND_CANDIDATES_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_FRONTEND_CANDIDATES_MAX", "30"))
TEST_COVERAGE_MANIFESTS_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_MANIFESTS_MAX", "10"))

# gates/test_coverage_gate.py's per-class/per-file coverage feedback: how many partially-covered
# branch line numbers to list inline in one class's feedback string.
TEST_COVERAGE_UNCOVERED_LINES_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_UNCOVERED_LINES_MAX", "20"))

# gates/test_coverage_gate.py's _run_coverage_via_ghcp: how many coverage-contract entries to
# actually parse per attempt (both the first pass and the one re-discovery retry share this cap) --
# "bounded: dozens of entries is itself suspect" per the file's own comment at this call site.
TEST_COVERAGE_CONTRACT_ENTRIES_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_CONTRACT_ENTRIES_MAX", "10"))

# gates/test_coverage_gate.py's coverage-gap feedback: how many gaps get a quoted source excerpt,
# and how many of one gap's own branch line numbers get quoted -- the file used the same literal
# for both before this was named, so one constant here matches that, not two.
TEST_COVERAGE_GAP_DETAIL_MAX = int(os.environ.get("AIDW_TEST_COVERAGE_GAP_DETAIL_MAX", "6"))

# gates/test_coverage_gate.py's coverage floor: both line and branch coverage must meet this
# percentage for the deterministic_verify to pass. Read by metrics_nodes.py too (imported by name,
# not retyped). Raising it makes the gate strict enough to block on legitimately-untested code that
# passed before; lowering it lets code with weaker tests through. Env var name predates this
# constant's move into config.py -- kept as-is (not AIDW_-prefixed) so an existing deploy's
# MIN_COVERAGE_PERCENT keeps working unchanged.
MIN_COVERAGE_PERCENT = float(os.environ.get("MIN_COVERAGE_PERCENT", "95.0"))

# gates/test_coverage_gate.py's sandbox-relative path for the model-authored coverage-command
# contract (COVERAGE_COMMANDS_PATH) and the report formats _replay_coverage_contract knows how to
# parse (_CONTRACT_FORMATS -- cobertura XML, istanbul JSON summary). Read by ac_eval.py,
# exit_nodes.py and metrics_nodes.py too. Adding a format string here does nothing on its own --
# _parse_cobertura_counts/_parse_istanbul_counts must actually support it.
COVERAGE_COMMANDS_PATH = os.environ.get("AIDW_COVERAGE_COMMANDS_PATH", ".ai-dev-workflow/coverage-commands.json")
CONTRACT_FORMATS = tuple(
    f.strip() for f in os.environ.get("AIDW_CONTRACT_FORMATS", "cobertura,istanbul-json-summary").split(",") if f.strip()
)

# gates/test_coverage_gate.py's _replay_coverage_contract: wall-clock cap (via `timeout`) on ONE
# coverage command's replay run. Env var name predates this constant's move into config.py -- kept
# as-is so an existing deploy's REPO_SCAN_COVERAGE_TIMEOUT_SECONDS keeps working unchanged.
TEST_COVERAGE_REPLAY_TIMEOUT_SECONDS = int(os.environ.get("REPO_SCAN_COVERAGE_TIMEOUT_SECONDS", "600"))

# graph.py's make_draft_node infra-exhaustion handler: how much of the raw exception message to
# keep as stage["last_infra_error"]. Tail-only (not head+tail like the pairs above) is correct
# here -- the content is a short exception string, not a multi-item list, so there's no "start of a
# long list" to lose.
GRAPH_INFRA_ERROR_CHARS = int(os.environ.get("AIDW_GRAPH_INFRA_ERROR_CHARS", "2000"))

# graph.py's metrics-report/e2e-outcome prompt messages: how many failed-test entries to inline,
# and the JSON-serialisation budget (via _bounded_json, which truncates honestly -- see its own
# docstring -- rather than cutting a serialised payload mid-token) for the metrics-compute and
# e2e-summary payloads respectively. Raising the JSON limits lets a model see more of a large
# payload at the cost of prompt size; MARGIN is _bounded_json's own reserve for its wrapper JSON
# (_truncated/_original_chars/_note keys) around the clipped preview -- it must stay big enough
# that the wrapper itself never exceeds `limit`.
GRAPH_FAILED_TESTS_MAX = int(os.environ.get("AIDW_GRAPH_FAILED_TESTS_MAX", "10"))
GRAPH_METRICS_JSON_MAX_CHARS = int(os.environ.get("AIDW_GRAPH_METRICS_JSON_MAX_CHARS", "8000"))
GRAPH_E2E_SUMMARY_JSON_MAX_CHARS = int(os.environ.get("AIDW_GRAPH_E2E_SUMMARY_JSON_MAX_CHARS", "4000"))
GRAPH_BOUNDED_JSON_MARGIN_CHARS = int(os.environ.get("AIDW_GRAPH_BOUNDED_JSON_MARGIN_CHARS", "240"))

# graph.py's make_verify_node INFRA RETRY / REDRAFT log lines: how much of a verify result's own
# feedback to log. 1200, not a smaller value: an adversarial-compliance rejection is a LIST of
# findings, and a 300-char cap once stopped inside the first one, making a thrashing stage
# undiagnosable from the log alone (observed live). Log-only -- the full, untruncated feedback is
# still stored in stage["last_verification"] and used unmodified for the actual redraft prompt.
GRAPH_FEEDBACK_LOG_PREVIEW_CHARS = int(os.environ.get("AIDW_GRAPH_FEEDBACK_LOG_PREVIEW_CHARS", "1200"))

# git_ops.py's GitHub REST calls (open/update PR, delete branch): how much of a failed response
# body to log -- these are log-and-continue failures a human debugs from server logs, never fed
# back into a model. Also the shared httpx client timeout for all three calls; raising it tolerates
# a slower GitHub API response before giving up, at the cost of a slower log-and-continue on a
# genuine outage.
GIT_OPS_API_ERROR_PREVIEW_CHARS = int(os.environ.get("AIDW_GIT_OPS_API_ERROR_PREVIEW_CHARS", "300"))
GIT_OPS_HTTP_TIMEOUT_SECONDS = float(os.environ.get("AIDW_GIT_OPS_HTTP_TIMEOUT_SECONDS", "30.0"))

# git_ops.py's push_head: how much of a failed `git push`'s own stderr/stdout to keep in
# _LAST_PUSH's "error" field, surfaced to the session/UI as why the push didn't happen.
GIT_OPS_PUSH_ERROR_TAIL_CHARS = int(os.environ.get("AIDW_GIT_OPS_PUSH_ERROR_TAIL_CHARS", "500"))

# git_ops.py's generated-.gitignore detection log line: how many detected paths to list before
# summarizing the rest with "...".
GIT_OPS_GITIGNORE_PREVIEW_MAX = int(os.environ.get("AIDW_GIT_OPS_GITIGNORE_PREVIEW_MAX", "8"))

# exit_nodes.py's SBOM-diff section: how many added/removed/version-changed dependency names to
# list inline before summarizing the rest as "...and N more".
EXIT_SBOM_DIFF_PREVIEW_MAX = int(os.environ.get("AIDW_EXIT_SBOM_DIFF_PREVIEW_MAX", "15"))
# exit_nodes.py's _failure_headline: length of the single-line headline bullet shown above the
# full terminal-failure code block (see EXIT_FAILURE_DETAIL_HEAD/TAIL_CHARS above for that block).
EXIT_FAILURE_HEADLINE_CHARS = int(os.environ.get("AIDW_EXIT_FAILURE_HEADLINE_CHARS", "300"))
# exit_nodes.py's tool-run failure notes: how much of one failed tool run's own error/summary text
# to inline in the exit report's tooling section.
EXIT_TOOL_ERROR_SNIPPET_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_ERROR_SNIPPET_CHARS", "80"))
# exit_nodes.py's _md_cell default column width for a markdown table cell (used as-is by the
# known-gap cell at limit 110) and the per-column widths in the security-findings/tools tables
# (tools/rule-id/title/where/version/notes). Each is independently tunable because the columns
# hold very different content (a CVE id vs. a finding title vs. free-text notes) -- raising one
# widens that column's cell before "..." kicks in, at the cost of a wider markdown table.
EXIT_MD_CELL_DEFAULT_CHARS = int(os.environ.get("AIDW_EXIT_MD_CELL_DEFAULT_CHARS", "90"))
EXIT_KNOWN_GAP_CELL_CHARS = int(os.environ.get("AIDW_EXIT_KNOWN_GAP_CELL_CHARS", "110"))
EXIT_HEALTH_BASIS_CELL_CHARS = int(os.environ.get("AIDW_EXIT_HEALTH_BASIS_CELL_CHARS", "100"))
EXIT_FINDING_TOOLS_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_TOOLS_CELL_CHARS", "40"))
EXIT_FINDING_RULE_ID_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_RULE_ID_CELL_CHARS", "40"))
EXIT_FINDING_TITLE_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_TITLE_CELL_CHARS", "70"))
EXIT_FINDING_WHERE_CELL_CHARS = int(os.environ.get("AIDW_EXIT_FINDING_WHERE_CELL_CHARS", "60"))
EXIT_TOOL_VERSION_CELL_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_VERSION_CELL_CHARS", "45"))
EXIT_TOOL_NOTES_CELL_CHARS = int(os.environ.get("AIDW_EXIT_TOOL_NOTES_CELL_CHARS", "60"))
# exit_nodes.py's security-findings table: how many findings get their own row before the rest
# collapse into one "...and N more, see repo-scan-latest.json" row.
EXIT_FINDINGS_TABLE_CAP = int(os.environ.get("AIDW_EXIT_FINDINGS_TABLE_CAP", "60"))
# exit_nodes.py's user-stories/AC table: how many of one row's own linked test ids to list inline
# before summarizing the rest as "+N more".
EXIT_TEST_IDS_PREVIEW_MAX = int(os.environ.get("AIDW_EXIT_TEST_IDS_PREVIEW_MAX", "3"))
# exit_nodes.py's stale-snapshot divergence log line: how much of each stale reason to include.
EXIT_STALE_REASON_LOG_CHARS = int(os.environ.get("AIDW_EXIT_STALE_REASON_LOG_CHARS", "120"))

# e2e_nodes.py's blank-screenshot failure item: how many blank-screenshot filenames to list inline
# before summarizing the rest as "and N more".
E2E_BLANK_SCREENSHOTS_PREVIEW_MAX = int(os.environ.get("AIDW_E2E_BLANK_SCREENSHOTS_PREVIEW_MAX", "5"))
# e2e_nodes.py's lighthouse audit summary: length of one failing audit's title/selector string
# (both truncated inside the sandboxed extraction script, _LH_EXTRACT_PY, before the JSON crosses
# the exec boundary), and how many failing audits survive -- once per route inside the extraction
# script, then again across all routes' worst-of results at the Python-side aggregation in
# _run_lighthouse. Same value shared by both stages by design, not coincidence: raising it shows
# more/longer failing-audit detail to the e2e fix model at both stages.
E2E_LIGHTHOUSE_AUDIT_TEXT_CHARS = int(os.environ.get("AIDW_E2E_LIGHTHOUSE_AUDIT_TEXT_CHARS", "120"))
E2E_LIGHTHOUSE_FAILING_AUDITS_MAX = int(os.environ.get("AIDW_E2E_LIGHTHOUSE_FAILING_AUDITS_MAX", "12"))

# gates/diagram_gate.py's _mermaid_error_summary: companion to DIAGRAM_ERROR_SUMMARY_HEAD/TAIL_CHARS
# above, but for the HEAD-lines extraction that pulls mmdc's actionable "Parse error on line N"
# text out of its output before the head+tail raw capture even runs -- how many of the output's own
# meaningful lines to keep, and the char cap on the joined result.
DIAGRAM_ERROR_SUMMARY_LINES_MAX = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_LINES_MAX", "10"))
DIAGRAM_ERROR_SUMMARY_JOINED_CHARS = int(os.environ.get("AIDW_DIAGRAM_ERROR_SUMMARY_JOINED_CHARS", "700"))

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
CLI_AGENT_TURN_TIMEOUT_SECONDS = int(os.environ.get("CLI_AGENT_TURN_TIMEOUT_SECONDS", "5400"))

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
