#!/usr/bin/env node
// Stop hook: catches verify_ac_to_tests's OWN hard rules (write_scope_gate.py) that until now had
// ZERO same-turn coverage -- only the test-*content*-quality axis (test_quality_checks.py, via
// check-test-quality-stop.mjs/check-testid-locators-stop.mjs) was hook-covered. Ledger-tamper
// detection, retired/deferred-AC test residue, completed-AC test protection, the unattributed-test
// attribution diagnostic, the per-AC ui_relevant-needs-e2e depth check, the write-scope violation
// check, the "wrote no real test files" check, the test-pyramid (e2e-only/missing-e2e) classifiers,
// and the playwright.config.ts screenshot-mode check were ALL entirely graph-side until now -- a
// violation was only ever caught a full draft->audit->verify round-trip later. Task 13, items 2+3.
//
// WHY TWO PYTHON SUBPROCESSES, NOT A JS PORT: every check below lives in
// gates/ac_residue_checks.py and gates/write_scope_checks.py (extracted 2026-09-30, Task 13, from
// ac_coverage_gate.py/write_scope_gate.py, which import both back unchanged -- see each module's
// own docstring), COPY'd in below UNMODIFIED and stdlib-only, so this hook shells out to the REAL
// implementations instead of a second, independently-drifting copy.
//
// GATED ON AIDW_STAGE=ac-to-tests ONLY -- every one of these rules is specific to this stage's own
// deterministic_verify (ac_coverage_gate.check_ac_coverage / write_scope_gate.verify_ac_to_tests).
//
// AIDW_BASELINE_COMMIT (Task 5) is what unblocks item 3's write-scope-gated checks: they need the
// stage's own changed-paths set (untracked files + `git diff --name-only {baseline}`), which no
// same-turn hook could compute before that env var existed. Shape-validated before it reaches an
// interpolated shell string, same posture as check-remediation-stop.mjs's identical use of it.
//
// KNOWN, DELIBERATE GAPS (never for the real gate, which still runs the full versions of all of
// these at verify time -- only for THIS same-turn nudge):
//   - `check_write_scope`'s own retirement carve-out (a deleted test file naming a retired AC is
//     in-scope by definition) is not reproduced here -- would need a `git show <baseline>:<path>`
//     probe per violating path. `ponytail: a legitimate retirement-delete can read as a violation
//     nudge here; upgrade by passing retired AC ids + each violating path's baseline content if
//     this proves noisy live.`
//   - the e2e-presence check runs `_classify_e2e_paths(changed_paths, null)` -- that function's OWN
//     built-in graceful degrade for "no confidently-resolved web root", not the strict
//     tech-stack-resolved membership check the real gate applies (write_scope_checks.py can't
//     import tech_stack_signals.py -- see that module's own docstring). Same reasoning as every
//     other same-turn hook in this image: a best-effort nudge, never the authority.
//   - `hasUi` below is a SEPARATE, minimal local read of the tech-stack JSON (just enough for one
//     boolean), not tech_stack_signals.frameworks_have_ui itself -- UI_FRAMEWORK_MARKERS is
//     duplicated by hand below for the same reason `_TEST_FILE_LISTING` is in every sibling hook
//     that needs it: KEEP THIS IN SYNC WITH tech_stack_signals.py'S UI_FRAMEWORK_MARKERS BY HAND if
//     that list ever changes.
//   - `coverage_plan`/ui_relevant flags come from `.ai-dev-workflow/05-ac-to-tests.draft.json`,
//     which the ORCHESTRATOR persists between turns, not the model itself -- so on this stage's
//     very first draft turn the file doesn't exist yet and this one check is silently skipped
//     (fail-open, same posture check-ledger-sync-stop.mjs's own draft-file gate already accepts).
import { readFileSync, existsSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-ac-residue-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "ac-to-tests") process.exit(0);

const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
const AC_TO_TESTS_DRAFT_PATH = ".ai-dev-workflow/05-ac-to-tests.draft.json";
const TECH_STACK_PATHS = [".ai-dev-workflow/02-tech-stack.approved.json", ".ai-dev-workflow/02-tech-stack.draft.json"];

// Ported verbatim from tech_stack_signals.py's own UI_FRAMEWORK_MARKERS.
const UI_FRAMEWORK_MARKERS = ["react", "vue", "angular", "blazor", "svelte", "next", "nuxt", "flutter", "swiftui", "jetpack compose"];

const TEST_FILE_LISTING =
  "git ls-files -co --exclude-standard | grep -iE '(test|spec)' " +
  "| grep -viE '(^|/)(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|test-?results|coverage|\\.ai-dev-workflow|agent-work)/' " +
  "| grep -viE '\\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|mp4|webm|woff2?|ttf|eot|dll|exe|so|dylib|pyc|class|jar)$'";

// Same cap as check-test-quality-stop.mjs/check-testid-locators-stop.mjs, used ONLY for the
// depth-scan-shaped checks below (unattributed_tests, ui_relevant e2e/count_tests_per_ac) --
// ac_coverage_gate.py's own check_ac_coverage feeds those the SAME capped `head -60` listing (that
// module's own comment: "head -60 is legitimate HERE"). The residue/protection checks (retired/
// deferred residue, completed-AC protection) use a SEPARATE, uncapped gather below instead --
// capping those would silently misreport a completed AC's surviving regression test as deleted
// once a repo grows past 60 test files, a same-turn hard-block for something the real (uncapped)
// gate correctly allows. See ac_coverage_gate.py's own comment on this exact split.
const TEST_FILE_LISTING_CAP = 60;

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

// --- test_files: same listing+cap every sibling ac-to-tests hook already uses ------------------
// This CAPPED set is correct for the depth-scan-shaped checks (unattributed_tests, ui_relevant
// e2e/count_tests_per_ac) -- ac_coverage_gate.py's own check_ac_coverage feeds those the SAME
// capped `head -60` listing (see that module's own comment: "head -60 is legitimate HERE").
let testFiles = {};
const listing = run(`(${TEST_FILE_LISTING}) | head -${TEST_FILE_LISTING_CAP}`);
for (const path of listing.split("\n").map((l) => l.trim()).filter(Boolean)) {
  try {
    testFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
  } catch {
    // race with the model's own in-flight write -- not this hook's problem to report.
  }
}

// --- residue_test_files: the UNCAPPED twin, for retired/deferred residue + completed-AC
// protection ONLY -- mirrors ac_coverage_gate.py's own two-listing split exactly (that module's
// comment: "the grep-only residue/protection checks below use the uncapped _TEST_FILE_LISTING --
// a cap there silently skipped every test file past the 60th"). completed_ac_protection_violations
// is an ABSENCE-IMPLIES-VIOLATION check: a completed AC's regression test genuinely surviving past
// position 60 (plausible in a maturing multi-ticket codebase -- this hook runs on every ac-to-tests
// cycle, not just ticket 1) must never read as "deleted" here just because the capped listing
// didn't reach it -- that would same-turn hard-block something the real (uncapped) gate correctly
// allows, exactly the asymmetry this split exists to prevent.
let residueTestFiles = {};
const uncappedListing = run(`(${TEST_FILE_LISTING})`);
for (const path of uncappedListing.split("\n").map((l) => l.trim()).filter(Boolean)) {
  try {
    residueTestFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
  } catch {
    // race with the model's own in-flight write -- not this hook's problem to report.
  }
}

// --- ledger: entries (residue/protection/attribution) + own uncommitted-diff (integrity) --------
const ledgerDoc = readJson(LEDGER_PATH);
const ledgerEntries = Array.isArray(ledgerDoc?.entries) ? ledgerDoc.entries : [];
const ledgerDiff = run(`git diff --name-only -- ${LEDGER_PATH}`);

// --- playwright config: first match, content only (screenshot-mode check) -----------------------
let playwrightConfig = null;
const configListing = run("git ls-files -co --exclude-standard -- '*playwright.config.*'");
const firstConfig = configListing.split("\n").map((l) => l.trim()).filter(Boolean)[0];
if (firstConfig) {
  try {
    playwrightConfig = readFileSync(`${cwd}/${firstConfig}`, "utf8");
  } catch {
    playwrightConfig = null;
  }
}

// --- coverage_plan: this stage's own persisted draft, one turn stale at worst (see header) ------
const draft = readJson(AC_TO_TESTS_DRAFT_PATH);
const coveragePlan = draft?.test_suite?.coverage_plan ?? null;

// --- has_ui: does this repo have a UI framework at all, per its own tech-stack record ------------
let hasUi = false;
for (const path of TECH_STACK_PATHS) {
  const techStack = readJson(path);
  if (!techStack) continue;
  const frameworks = Array.isArray(techStack.frameworks?.values)
    ? techStack.frameworks.values
    : Array.isArray(techStack.frameworks)
      ? techStack.frameworks
      : [];
  if (frameworks.length > 0) {
    const lowered = frameworks.map((f) => String(f).toLowerCase());
    hasUi = UI_FRAMEWORK_MARKERS.some((marker) => lowered.some((fw) => fw.includes(marker)));
    break;
  }
}

// --- no_eligible_work: every one of this ticket's own active/revised ACs already has a
// coded_run_id (or there are none) -- mirrors verify_ac_to_tests's work-queue-scoping guard so a
// legitimate deletion-only/all-delivered ticket isn't nagged to "write tests" for forbidden re-work.
const activeAcEntries = ledgerEntries.filter(
  (e) => e?.kind === "acceptance_criterion" && (e.status === "active" || e.status === "revised"),
);
const noEligibleWork = ledgerEntries.length > 0 && activeAcEntries.every((e) => e.coded_run_id);

// --- changed_paths: untracked + diff against AIDW_BASELINE_COMMIT (Task 5) ----------------------
// Shape-validated before it reaches an interpolated shell string -- an env var, however this
// pipeline sets it today, must never be trusted into a shell command unchecked (same posture as
// check-remediation-stop.mjs's identical use of this exact env var).
const rawBaselineCommit = process.env.AIDW_BASELINE_COMMIT;
const baselineCommit = rawBaselineCommit && /^[0-9a-f]{7,40}$/i.test(rawBaselineCommit) ? rawBaselineCommit : null;
let changedPaths = [];
if (baselineCommit) {
  const diffOut = run(`git diff --name-only ${baselineCommit} -- . && git ls-files --others --exclude-standard`);
  changedPaths = [...new Set(diffOut.split("\n").map((l) => l.trim()).filter(Boolean))].sort();
}

// --- shell out to both python check-hooks ---------------------------------------------------------
function runCheckHook(script, payload) {
  const proc = spawnSync("python3", [`/opt/aidw-hooks/${script}`, "--check-hook"], {
    input: JSON.stringify(payload),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, `${script} subprocess failed, timed out, or produced no output`, cwd);
    return null; // infra gap -- never a false rejection
  }
  try {
    return JSON.parse(proc.stdout);
  } catch {
    reportFailOpen(HOOK_NAME, stage, `unparsable ${script} output`, cwd);
    return null;
  }
}

const residue = runCheckHook("ac_residue_checks.py", {
  ledger_diff: ledgerDiff,
  ledger_entries: ledgerEntries,
  test_files: testFiles,
  residue_test_files: residueTestFiles,
  playwright_config: playwrightConfig,
  coverage_plan: coveragePlan,
});

const scope = baselineCommit
  ? runCheckHook("write_scope_checks.py", { changed_paths: changedPaths, has_ui: hasUi, no_eligible_work: noEligibleWork })
  : null; // no baseline captured -- nothing to diff against, same "fail open, not a false positive" contract check_write_scope itself uses for baseline_commit=None

const problems = [];

if (residue) {
  problems.push(...(residue.ledger_integrity || []));
  problems.push(...(residue.retired_residue || []));
  problems.push(...(residue.deferred_residue || []));
  problems.push(...(residue.completed_protection || []));

  const orphanPaths = Object.keys(residue.unattributed_tests || {}).sort();
  if (orphanPaths.length > 0) {
    const total = orphanPaths.reduce((sum, p) => sum + residue.unattributed_tests[p], 0);
    problems.push(
      `ATTRIBUTION WARNING: ${total} test declaration(s) carry no recognisable AC id (${orphanPaths.join(", ")}). ` +
        "Every test must name its criterion in its own name (e.g. `Test_US_0001_2_...`, " +
        "`TestUS00012...`, or `[US-0001.2] ...` in a Playwright title) -- a test that covers a " +
        "criterion without naming it cannot be credited to it.",
    );
  }

  if (residue.screenshot_missing) {
    problems.push(
      "playwright.config.ts does not set `screenshot: 'on'` in its `use` block -- Playwright's " +
        "default (`only-on-failure`) captures nothing for a passing suite. Add `screenshot: 'on'` " +
        "to `use` so passing tests still yield visual evidence; the e2e stage's wireframe-coverage " +
        "gate depends on it.",
    );
  }

  const missingE2e = residue.ui_relevant_missing_e2e || {};
  const missingE2eAcs = Object.keys(missingE2e).sort();
  if (missingE2eAcs.length > 0) {
    const named = missingE2eAcs.map((ac) => `${ac}: ${missingE2e[ac].join("; ")}`).join(" | ");
    problems.push(`these criteria are marked user-facing but have no end-to-end test yet: ${named}`);
  }
}

if (scope) {
  if ((scope.violating_paths || []).length > 0) {
    problems.push(
      `these files are outside the test-only write scope for this stage: ${scope.violating_paths.join(", ")} -- ` +
        "only test files, test configs, and test setup files may be created or modified here; " +
        "remove or move them now, in this same turn.",
    );
  }
  if (scope.wrote_nothing_real) {
    problems.push(
      "You have not created any test files yet -- the working tree has no changes beyond pipeline " +
        "artifacts. Write the actual test files with your file tools (create/edit) before finishing.",
    );
  }
  if (scope.e2e_only) {
    problems.push(
      "Every test written so far is a Playwright end-to-end spec. A browser test cannot prove the " +
        "rules beneath the UI, and a unit runner cannot execute it. Add unit and/or integration/" +
        "subcutaneous tests for the same criteria too, and keep e2e for genuine user journeys.",
    );
  }
  if (scope.missing_e2e) {
    problems.push(
      "This stack has a UI framework but no working Playwright end-to-end spec has been written " +
        `yet (${scope.e2e_diagnosis}). Add a playwright.config.ts beside the web app plus at least ` +
        "one spec under tests/e2e/ covering the primary user journeys.",
    );
  }
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "This stage has problems the deterministic write-scope/AC-coverage gate will reject at verify " +
    "time -- fix them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
