#!/usr/bin/env node
// Stop hook: runs metrics-exit's own deterministic verdict logic (gates/exit_readiness_checks.py,
// the SAME pure implementation exit_nodes.verify_exit_readiness delegates to) against THIS turn's
// own already-on-disk artifacts, for a same-turn signal, instead of waiting a full
// draft->verify round-trip for verify_exit_readiness to report the identical thing.
//
// WHY THIS STAGE WAS INITIALLY ASSESSED "NO PURE CORE" AND WHY THAT WAS WRONG: verify_exit_readiness
// LOOKS coupled (it calls app_discovery.collect_evidence, reads/writes the sandbox filesystem) but
// every one of those calls is deliberately STATIC: collect_evidence's own docstring says "nothing
// here launches an app... not verified by execution" (a bounded `find`+`cat`+regex classification
// over already-checked-out source), and _list_screenshots is one `ls` on a directory the EARLIER e2e
// stage already wrote before its own turn ended. By the time metrics-exit's turn starts, every input
// this hook needs is already sitting on disk -- nothing here boots a process or opens a port.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: the verdict logic (manifest presence, screenshot presence,
// metrics/regression-gate/readme check, auth check, targeted-fix-unresolved fold-in, the
// stale-carried-over-blocker filter, and the final merge_ready decision) lives in
// gates/exit_readiness_checks.py (extracted 2026-09-30, Task 14, from exit_nodes.py, which imports
// it back UNMODIFIED -- see that module's own docstring), COPY'd in below UNMODIFIED and
// stdlib-only, so this hook shells out to the REAL implementation instead of a second,
// independently-drifting copy. This JS file does ONLY file I/O (manifest.json, metrics-latest.json,
// targeted-fix-unresolved.json, coverage-commands.json, the tech-stack file, the screenshots
// directory, and a bounded git-tracked-file scan for the app-discovery re-scan) and env/transcript
// reads; every DECISION is made by the python subprocess.
//
// NO ON-DISK REPORT ARTIFACT FOR content_dict: this stage's MergeReadinessReport
// (merge_ready/blocking_reasons/risk_notes) is the model's own final turn message, not a repo file
// (graph.py's make_draft_node calls structured_output.ainvoke_structured, whose whole contract is
// "respond with a single JSON object matching the schema as your FINAL message"). So this hook reads
// the SAME transcript check-adversarial-stop.mjs/check-full-read-stop.mjs already rely on
// (`input.transcript_path`) via the shared lib/read-final-json.mjs helper (Task 10) -- reused
// UNCHANGED, not reimplemented; `parsed.readiness`/`parsed.report` is the exact field-access pattern
// that hook already established for this same schema shape (ExitDraftResponse/MergeReadinessReport,
// schemas_exit.py), confirmed during Task 10's review to match.
//
// STAGE-SCOPED VIA AIDW_STAGE == "metrics-exit" (mirrors check-adversarial-stop.mjs's own stage
// gate) -- this hook's file reads are meaningless for any other stage.
//
// AIDW_RUN_ID (Task 5) is what unblocks the run_id-gated half of these checks (metrics/regression
// gate, auth, targeted-fix-unresolved, screenshots) -- exactly the same "not yet wired up, not a
// regression" contract Task 5's own env-var comment documents; a turn with no AIDW_RUN_ID set simply
// skips that half rather than guessing.
//
// AIDW_AUTH_GATE: threaded into the sandbox env the exact same trivial way check-coverage-stop.mjs
// already threads MIN_COVERAGE_PERCENT in -- a bare `process.env.AIDW_AUTH_GATE` read, same
// default/parsing convention as config.py's own `os.environ.get("AIDW_AUTH_GATE", "1")` (falsy set:
// "0"/"false"/"no"/"off"/"").
//
// baseline_commit is NOT read here: verify_exit_readiness's own `baseline_commit` parameter is
// never referenced anywhere in that function's body (confirmed by reading it in full) -- there is
// nothing for this hook to diff against either.
//
// KNOWN, DELIBERATE GAPS (never for the real gate, which still runs the full versions of all of
// these at verify time -- only for THIS same-turn nudge):
//   - The manifest-completion re-scan (app_check.apps) reuses exit_readiness_checks.py's own
//     bounded, hand-ported subset of app_discovery.classify_candidates (see that module's own
//     docstring for the exact, deliberately narrow scope -- only the marker names classify_candidates
//     actually branches on). `test_command` completion only tries the small
//     combined_test_command_from_apps fallback, never ac_coverage_gate.resolve_test_command's
//     tech-stack-based guessing (that module pulls in chat_model/stack_runner/tech_stack_signals and
//     is not reasonably portable here) -- `ponytail: at most a redundant "add a test_command" nudge
//     on a field this stage's own docstring calls purely documentary; upgrade only if this proves
//     noisy live.`
//   - `is_ui` is a SEPARATE, minimal local read of the tech-stack file (just enough for one
//     boolean), not tech_stack_signals.frameworks_have_ui itself (that module pulls in
//     schemas.TechStack's pydantic validator chain) -- UI_FRAMEWORK_MARKERS is duplicated by hand
//     below, same convention check-ac-residue-stop.mjs's own `hasUi` already uses: KEEP THIS IN SYNC
//     WITH tech_stack_signals.py'S UI_FRAMEWORK_MARKERS BY HAND if that list ever changes.
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";
import { extractFinalJson } from "./lib/read-final-json.mjs";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-exit-readiness-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "metrics-exit") process.exit(0);

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

if (!input.transcript_path) process.exit(0); // nothing to read yet

let transcriptText;
try {
  transcriptText = readFileSync(input.transcript_path, "utf8");
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable transcript file", cwd);
  process.exit(0); // unreadable transcript -- fail open, same contract as every other transcript-reading hook
}

const parsed = extractFinalJson(transcriptText);
// No parseable final JSON yet, or the model isn't done this attempt, or it's still asking a
// clarifying question (readiness=false is routine, not a failure) -- nothing to check yet.
if (!parsed || parsed.readiness !== true || !parsed.report) process.exit(0);

const runId = process.env.AIDW_RUN_ID || "";

function readJson(relPath) {
  const abs = `${cwd}/${relPath}`;
  if (!existsSync(abs)) return null;
  try {
    return JSON.parse(readFileSync(abs, "utf8"));
  } catch {
    return null; // present but unreadable/malformed -- treated as absent, never a crash
  }
}

function run(cmd) {
  try {
    return execSync(cmd, { cwd, encoding: "utf8", shell: "/bin/bash" });
  } catch {
    return "";
  }
}

const MANIFEST_PATH = ".ai-dev-workflow/manifest.json";
const manifest = readJson(MANIFEST_PATH) || {};

const COVERAGE_COMMANDS_PATH = process.env.AIDW_COVERAGE_COMMANDS_PATH || ".ai-dev-workflow/coverage-commands.json";
const coverageCommandsDoc = readJson(COVERAGE_COMMANDS_PATH);
const coverageEntries = Array.isArray(coverageCommandsDoc?.entries) ? coverageCommandsDoc.entries : null;

const METRICS_PATH = ".ai-dev-workflow/metrics-latest.json";
const metrics = readJson(METRICS_PATH) || {};

const TARGETED_FIX_UNRESOLVED_PATH = ".ai-dev-workflow/targeted-fix-unresolved.json";
const targetedFix = readJson(TARGETED_FIX_UNRESOLVED_PATH) || {};

const HISTORY_DIR = ".ai-dev-workflow/history";
let screenshotCount = 0;
if (runId) {
  try {
    screenshotCount = readdirSync(`${cwd}/${HISTORY_DIR}/${runId}-screens`).filter(Boolean).length;
  } catch {
    screenshotCount = 0; // directory doesn't exist yet -- e2e may not have written it, or genuinely zero
  }
}

// --- is_ui: minimal local read, same pattern check-ac-residue-stop.mjs's own hasUi uses (see this
// hook's header for why tech_stack_signals.frameworks_have_ui itself can't be imported here) ------
const TECH_STACK_APPROVED_PATH = ".ai-dev-workflow/02-tech-stack.approved.json";
// Ported verbatim from tech_stack_signals.py's own UI_FRAMEWORK_MARKERS.
const UI_FRAMEWORK_MARKERS = ["react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose"];
let isUi = false;
const techStack = readJson(TECH_STACK_APPROVED_PATH);
if (techStack) {
  const frameworks = Array.isArray(techStack.frameworks?.values)
    ? techStack.frameworks.values
    : Array.isArray(techStack.frameworks)
      ? techStack.frameworks
      : [];
  const lowered = frameworks.map((f) => String(f).toLowerCase());
  isUi = UI_FRAMEWORK_MARKERS.some((marker) => lowered.some((fw) => fw.includes(marker)));
}

// --- app-discovery re-scan: candidate marker files, gathered here (JS does the file I/O, the
// python subprocess does the pure classification -- same split as check-coverage-stop.mjs's own
// replay-then-parse recipe). Only fires when app_check.apps is already empty (mirrors
// verify_exit_readiness's own conditional cost profile: this scan is skipped entirely once apps are
// recorded). Candidate NAMES ported by hand from app_discovery.py's own _CANDIDATE_NAMES -- narrowed
// to only the names gates/exit_readiness_checks.py's classify_candidates port actually branches on
// (see that module's own docstring for the full list of what's deliberately left out and why).
// KEEP IN SYNC BY HAND with app_discovery.py if its candidate-name list changes. -------------------
const CANDIDATE_NAME_PATTERNS = [
  /\.csproj$/, /(^|\/)host\.json$/, /(^|\/)package\.json$/, /(^|\/)app\.json$/,
  /(^|\/)capacitor\.config\.(json|ts)$/, /(^|\/)ionic\.config\.json$/, /(^|\/)AndroidManifest\.xml$/,
  /(^|\/)Program\.cs$/, /(^|\/)Startup\.cs$/, /(^|\/)manage\.py$/, /(^|\/)main\.py$/, /(^|\/)app\.py$/,
  /(^|\/)asgi\.py$/, /(^|\/)wsgi\.py$/, /(^|\/)pyproject\.toml$/, /(^|\/)requirements\.txt$/,
  /(^|\/)Procfile$/,
];
// Same exclusion list TEST_FILE_LISTING already uses in every sibling hook -- an untracked
// node_modules/vendor/dist tree can still surface plenty of package.json/pyproject.toml files that
// --exclude-standard alone won't catch on a repo whose own .gitignore is incomplete.
const CANDIDATE_FILE_LISTING =
  "git ls-files -co --exclude-standard " +
  "| grep -viE '(^|/)(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|test-?results|coverage|\\.ai-dev-workflow|agent-work)/'";
const CANDIDATE_FILE_CAP = 60; // same bound as app_discovery.py's own _MAX_CANDIDATE_FILES
const CANDIDATE_FILE_CHAR_CAP = 4000; // same bound as app_discovery.py's own _MAX_FILE_CHARS

let scannedFiles = {};
if (!(manifest.app_check?.apps || []).length) {
  const listing = run(CANDIDATE_FILE_LISTING)
    .split("\n")
    .map((l) => l.trim())
    .filter((p) => p && CANDIDATE_NAME_PATTERNS.some((re) => re.test(p)))
    .slice(0, CANDIDATE_FILE_CAP);
  for (const path of listing) {
    try {
      scannedFiles[path] = readFileSync(`${cwd}/${path}`, "utf8").slice(0, CANDIDATE_FILE_CHAR_CAP);
    } catch {
      // race with the model's own in-flight write -- not this hook's problem to report.
    }
  }
}

// --- AIDW_AUTH_GATE: same "0"/"false"/"no"/"off"/"" falsy-set convention as config.py's own
// os.environ.get("AIDW_AUTH_GATE", "1") parsing -----------------------------------------------
const authGateRaw = (process.env.AIDW_AUTH_GATE ?? "1").trim().toLowerCase();
const authGateEnabled = !["0", "false", "no", "off", ""].includes(authGateRaw);

const payload = {
  manifest,
  scanned_files: scannedFiles,
  coverage_entries: coverageEntries,
  is_ui: isUi,
  screenshot_count: screenshotCount,
  metrics,
  targeted_fix: targetedFix,
  run_id: runId,
  auth_gate_enabled: authGateEnabled,
  report: parsed.report,
};

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/exit_readiness_checks.py", "--check-hook"], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, "exit_readiness_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
  result = JSON.parse(proc.stdout);
} catch {
  reportFailOpen(HOOK_NAME, stage, "unparsable exit_readiness_checks.py output", cwd);
  process.exit(0);
}

if (result.passed) process.exit(0);

process.stderr.write(
  "This run has deterministic merge-readiness problems the exit gate will flag at verify time -- " +
    "fix them now, in this same turn, before finishing (or, where a listed reason is stale/no longer " +
    "true, revise your own report's merge_ready/blocking_reasons to match reality):\n" +
    (result.reasons || []).map((r) => `- ${r}`).join("\n") +
    "\n",
);
process.exit(2);
