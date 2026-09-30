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
// Same path check-plan-citations-stop.mjs already reads for the identical reason (this ticket's
// own approved AC ids) -- always present by the time ac-to-tests runs (plan cannot start, let
// alone approve, before specification is approved).
const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";

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

// Node's execSync default maxBuffer is 1 MiB, which silently throws (caught below, previously
// misread as "empty output") once a large repo's UNCAPPED test-file listing's combined stdout
// crosses it -- item 7 (final-review Fix Round 2). Each line here is a bare repo-relative PATH
// (tens of bytes), never file contents, so even an unusually large monorepo with, say, 100k test
// files (~60 bytes/line) lands around 6 MiB -- 32 MiB leaves a wide, deliberately generous margin
// above any realistic repo size without being large enough to mask a genuinely runaway command.
const RESIDUE_LISTING_MAX_BUFFER_BYTES = 32 * 1024 * 1024;

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

// Returns `null` (a distinct FAILURE sentinel, never conflated with "") when the command itself
// fails -- git not installed, not a repo, or (previously silently swallowed here, item 7) the
// uncapped listing overflowing execSync's own maxBuffer on a large repo. `""` still means exactly
// what it always meant: the command ran and genuinely produced no output. Every call site below
// must check for `null` and skip whatever check depends on that data source (reportFailOpen +
// omit), never treat a failure as an empty result -- silently reading "" is what turned a plain
// infra hiccup into "every completed AC's regression test looks deleted," a mass false block.
function run(cmd, options = {}) {
  try {
    return execSync(cmd, { cwd, encoding: "utf8", shell: "/bin/bash", ...options });
  } catch {
    return null;
  }
}

// --- test_files: same listing+cap every sibling ac-to-tests hook already uses ------------------
// This CAPPED set is correct for the depth-scan-shaped checks (unattributed_tests, ui_relevant
// e2e/count_tests_per_ac) -- ac_coverage_gate.py's own check_ac_coverage feeds those the SAME
// capped `head -60` listing (see that module's own comment: "head -60 is legitimate HERE").
let testFiles = {};
const listing = run(`(${TEST_FILE_LISTING}) | head -${TEST_FILE_LISTING_CAP}`);
if (listing === null) {
  // Safe either way even without this guard (unattributed_tests/ui_relevant_missing_e2e both
  // no-op on an empty test_files dict), but report it: a silent empty listing here is still an
  // infra gap worth knowing about, not a genuine "no test files" turn.
  reportFailOpen(HOOK_NAME, stage, "capped test-file listing command failed", cwd);
} else {
  for (const path of listing.split("\n").map((l) => l.trim()).filter(Boolean)) {
    try {
      testFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
    } catch {
      // race with the model's own in-flight write -- not this hook's problem to report.
    }
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
// residueListingFailed gates the three checks below that depend on this uncapped listing -- see
// the `problems.push` guard further down. Regardless of what ac_residue_checks.py computes for
// retired_residue/deferred_residue/completed_protection from whatever residueTestFiles ends up
// being (here, `{}`, since the loop below never runs), this hook must never surface those three
// specific results when the gathering itself failed -- completed_protection in particular is an
// ABSENCE-IMPLIES-VIOLATION check, so an empty/wrong residueTestFiles from a swallowed failure
// would read as "every completed AC's regression test was deleted," a mass false block (item 7).
let residueListingFailed = false;
const uncappedListing = run(`(${TEST_FILE_LISTING})`, { maxBuffer: RESIDUE_LISTING_MAX_BUFFER_BYTES });
if (uncappedListing === null) {
  residueListingFailed = true;
  reportFailOpen(
    HOOK_NAME, stage,
    "uncapped test-file listing command failed (git unavailable, not a repo, or output exceeded " +
      "the buffer) -- retired/deferred-residue and completed-AC-protection checks skipped this turn",
    cwd,
  );
} else {
  for (const path of uncappedListing.split("\n").map((l) => l.trim()).filter(Boolean)) {
    try {
      residueTestFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
    } catch {
      // race with the model's own in-flight write -- not this hook's problem to report.
    }
  }
}

// --- ledger: entries (residue/protection/attribution) + own uncommitted-diff (integrity) --------
const ledgerDoc = readJson(LEDGER_PATH);
const ledgerEntries = Array.isArray(ledgerDoc?.entries) ? ledgerDoc.entries : [];
const ledgerDiffRaw = run(`git diff --name-only -- ${LEDGER_PATH}`);
if (ledgerDiffRaw === null) {
  reportFailOpen(HOOK_NAME, stage, "ledger diff command failed -- ledger_integrity check skipped this turn", cwd);
}
// "" (command failure) reads to ledger_integrity_violations exactly like a genuine empty diff --
// safe-direction fail-open (no tamper detected), never a false block, so no separate skip needed.
const ledgerDiff = ledgerDiffRaw === null ? "" : ledgerDiffRaw;

// --- playwright config: first match, content only (screenshot-mode check) -----------------------
let playwrightConfig = null;
const configListing = run("git ls-files -co --exclude-standard -- '*playwright.config.*'");
if (configListing === null) {
  // playwrightConfig stays null either way (failure vs. genuinely no config found) -- downstream
  // ac_residue_checks.py already treats a null playwright_config as "skip screenshot_missing", so
  // this is inherently safe without further guarding; still reported for visibility.
  reportFailOpen(HOOK_NAME, stage, "playwright config listing command failed -- screenshot-mode check skipped this turn", cwd);
} else {
  const firstConfig = configListing.split("\n").map((l) => l.trim()).filter(Boolean)[0];
  if (firstConfig) {
    try {
      playwrightConfig = readFileSync(`${cwd}/${firstConfig}`, "utf8");
    } catch {
      playwrightConfig = null;
    }
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

// --- no_eligible_work: mirrors verify_ac_to_tests's own work-queue-scoping guard (write_scope_gate.py)
// so a legitimate deletion-only/all-delivered ticket isn't nagged to "write tests" for forbidden
// re-work. Item 7 (final-review Fix Round 2): this used to scope "eligible work" over EVERY ledger
// entry ever recorded, across every ticket this pipeline has ever run against this repo -- not just
// THIS ticket's own ACs, the scope the real gate actually uses. The real gate's own two building
// blocks (spec_ledger.own_ac_ids_from_specification / spec_ledger.eligible_ac_ids, both re-exported
// from gates/wireframe_linkage_checks.py, which is where Task 12 actually moved them) are ported by
// hand below -- KEEP IN SYNC BY HAND if either one's shape ever changes, same disclosed-duplication
// precedent as this file's own UI_FRAMEWORK_MARKERS/TEST_FILE_LISTING above (both are 5-line pure
// functions over a stable schema shape, not a business-rule surface worth a third python
// subprocess call in this same file).
const LIVE_AC_STATUSES = new Set(["active", "revised"]);

function ownAcIdsFromSpecification(specification) {
  const ids = new Set();
  for (const story of (specification && specification.user_stories) || []) {
    for (const ac of (story && story.acceptance_criteria) || []) {
      if (ac && typeof ac.id === "string") ids.add(ac.id);
    }
  }
  return ids;
}

function eligibleAcIds(entries, ownAcIds) {
  return entries
    .filter((e) => e?.kind === "acceptance_criterion" && LIVE_AC_STATUSES.has(e?.status) && ownAcIds.has(e?.id) && !e?.coded_run_id)
    .map((e) => e.id);
}

// write_scope_gate.py's own condition is `raw_spec is not None and not eligible_ac_ids(...)` --
// mirrored here via readJson's existing null-on-absent-or-malformed contract. One deliberate,
// disclosed divergence: the real gate distinguishes "file absent" (no_eligible_work=False) from
// "file present but malformed JSON" (no_eligible_work=True, own_ac_ids stays empty) via a
// try/except around json.loads; readJson collapses both to `null` like every other read in this
// file. The only practical difference is the rare malformed-approved-specification case, where
// this hook falls back to running the checks normally instead of staying silent -- still the
// safe direction (never a false block from unscoped-and-therefore-wrong data), just not a byte-
// for-byte match of that one edge case.
const approvedSpecDoc = readJson(SPECIFICATION_APPROVED_PATH);
const noEligibleWork =
  approvedSpecDoc !== null && eligibleAcIds(ledgerEntries, ownAcIdsFromSpecification(approvedSpecDoc)).length === 0;

// --- changed_paths: untracked + diff against AIDW_BASELINE_COMMIT (Task 5) ----------------------
// Shape-validated before it reaches an interpolated shell string -- an env var, however this
// pipeline sets it today, must never be trusted into a shell command unchecked (same posture as
// check-remediation-stop.mjs's identical use of this exact env var).
const rawBaselineCommit = process.env.AIDW_BASELINE_COMMIT;
const baselineCommit = rawBaselineCommit && /^[0-9a-f]{7,40}$/i.test(rawBaselineCommit) ? rawBaselineCommit : null;
// Item 7: a 5th run() call site the brief's own citation didn't enumerate (it names 4) -- found by
// re-reading the current file per this task's own instructions to verify every citation against
// actual code. Same failure-sentinel contract applies: null must skip the write-scope checks
// entirely (changedPathsFailed below), not silently proceed with an empty changedPaths, which
// would misread as "wrote nothing" (write_scope_checks.py's own wrote_nothing_real) -- a false
// block in the same direction this whole item exists to close, and previously also a crash
// (`.split` on `null`) once run() started returning the failure sentinel instead of "".
let changedPaths = [];
let changedPathsFailed = false;
if (baselineCommit) {
  const diffOut = run(`git diff --name-only ${baselineCommit} -- . && git ls-files --others --exclude-standard`);
  if (diffOut === null) {
    changedPathsFailed = true;
    reportFailOpen(HOOK_NAME, stage, "changed-paths diff command failed -- write-scope/test-pyramid checks skipped this turn", cwd);
  } else {
    changedPaths = [...new Set(diffOut.split("\n").map((l) => l.trim()).filter(Boolean))].sort();
  }
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

const scope = baselineCommit && !changedPathsFailed
  ? runCheckHook("write_scope_checks.py", { changed_paths: changedPaths, has_ui: hasUi, no_eligible_work: noEligibleWork })
  : null; // no baseline captured, or the diff command itself failed -- nothing trustworthy to diff against, same "fail open, not a false positive" contract check_write_scope itself uses for baseline_commit=None

const problems = [];

if (residue) {
  problems.push(...(residue.ledger_integrity || []));
  // Item 7: skipped entirely when the uncapped listing itself failed -- see residueListingFailed's
  // own comment above. Whatever ac_residue_checks.py computed for these three from an empty/wrong
  // residueTestFiles is not trustworthy and must never reach `problems`.
  if (!residueListingFailed) {
    problems.push(...(residue.retired_residue || []));
    problems.push(...(residue.deferred_residue || []));
    problems.push(...(residue.completed_protection || []));
  }

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
