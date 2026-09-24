#!/usr/bin/env node
// Stop hook: rejects two live-incident e2e-spec anti-patterns in the SAME turn that wrote them,
// instead of waiting a full redraft/fix round-trip for the deterministic gates that already
// enforce both (ac_coverage_gate.py's check_ac_coverage, test_coverage_gate.py's check_ac_depth,
// and e2e_nodes.py's e2e_run_node -- all three call the SAME
// gates/test_quality_checks.non_testid_locators/flaky_navigation_waits this hook shells out to)
// to report the same thing:
//
//   1. a Playwright locator that queries by role/text/label/tag/CSS instead of data-testid
//   2. a waitForNavigation()/networkidle wait that races a multi-hop redirect
//
// Root-caused 2026-09-21 (income-investor session f0fef8ba, run c1458b23): once e2e finally ran
// for real (a separate platform fix, same day), it hit a wall of failures that were not app bugs
// at all -- `page.locator("input")` matched a Next.js Server Action's own hidden
// `<input type="hidden" name="$ACTION_ID_...">`, rendered by the FRAMEWORK ahead of the real form
// field, instead of the field the test meant to check; `getByRole('button', { name: 'Save' })`
// missed because the real accessible name differed slightly from the guess. A role/text query
// LOOKS safer than a raw CSS selector but carries the identical risk: it matches whatever
// satisfies the query, not necessarily the element the test author had in mind. `data-testid` is
// the one surface nothing but the app's own author writes onto an element, so it is the only
// locator this convention allows for e2e/browser specs (unit/integration tests correctly keep
// using Testing Library's role/label queries -- a real accessibility check at THAT layer, not a
// liability -- see non_testid_locators' own docstring for why the rule is scoped to 'e2e' files
// only).
//
// Root-caused 2026-09-23 (income-investor commit fd6c91a, thread f0fef8ba): a shared signIn()
// test helper used Promise.all([page.waitForNavigation({ waitUntil: "networkidle" }),
// page.getByTestId(...).click()]) to wait out a multi-hop auth redirect. waitForNavigation() can
// resolve on the WRONG intermediate hop, and networkidle is not guaranteed to ever fire -- once
// this raced inside a helper every test in the spec calls, it regressed the whole suite (67/74
// passing -> 30/75), not just the sign-in test. See flaky_navigation_waits' own docstring.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: same reasoning as every other same-turn hook in this
// image (check-test-quality-stop.mjs, check-coverage-stop.mjs) -- gates/test_quality_checks.py is
// the ONE place this logic lives, pure stdlib, shipped unmodified to
// /opt/aidw-hooks/test_quality_checks.py; this hook's only job is finding the test files, handing
// their contents to it on stdin, and reporting what comes back on stdout.
//
// GATED ON THREE STAGES, NOT ONE: unlike check-test-quality-stop.mjs (ac-to-tests only) or
// check-coverage-stop.mjs (minimal-code-to-green only), this convention is enforced identically
// at all three stages that can write or edit an e2e spec -- ac-to-tests (authors them),
// minimal-code-to-green (can add/edit one for a wireframe gap found mid-stage), and e2e-fix
// (edits them directly when tests fail, which is exactly where the live incident above happened).
// A violation introduced at any of the three is the same real defect, so all three get the same
// same-turn nudge instead of only the one where it was first observed.
//
// TEST_FILE_LISTING below is ported VERBATIM from ac_coverage_gate.py's own `_TEST_FILE_LISTING`
// -- see check-test-quality-stop.mjs's own comment for the KEEP IN SYNC BY HAND caveat, which
// applies here identically.
import { readFileSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";

const TESTID_STAGES = new Set(["ac-to-tests", "minimal-code-to-green", "e2e"]);
if (!TESTID_STAGES.has(process.env.AIDW_STAGE)) process.exit(0);

const TEST_FILE_LISTING =
  "git ls-files -co --exclude-standard | grep -iE '(test|spec)' " +
  "| grep -viE '(^|/)(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|test-?results|coverage|\\.ai-dev-workflow|agent-work)/' " +
  "| grep -viE '\\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|mp4|webm|woff2?|ttf|eot|dll|exe|so|dylib|pyc|class|jar)$'";

// Matches ac_coverage_gate.py's own `head -60` on this exact listing -- same reasoning as
// check-test-quality-stop.mjs's identical cap.
const TEST_FILE_LISTING_CAP = 60;

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

let testPaths = [];
try {
  const out = execSync(`(${TEST_FILE_LISTING}) | head -${TEST_FILE_LISTING_CAP}`, {
    cwd,
    encoding: "utf8",
    shell: "/bin/bash",
  });
  testPaths = out.split("\n").map((l) => l.trim()).filter(Boolean);
} catch {
  process.exit(0); // git not available / not a repo yet -- fail open
}

if (testPaths.length === 0) process.exit(0); // no tests written yet this turn

const testFiles = {};
for (const path of testPaths) {
  try {
    testFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
  } catch {
    // race with the model's own in-flight write -- not this hook's problem to report.
  }
}
if (Object.keys(testFiles).length === 0) process.exit(0);

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/test_quality_checks.py", "--check-hook"], {
    input: JSON.stringify(testFiles),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) process.exit(0); // infra gap -- never a false rejection
  result = JSON.parse(proc.stdout);
} catch {
  process.exit(0);
}

// Two independent checks, one subprocess call, one JSON payload -- reported as separate
// paragraphs so a model fixing one doesn't read the other's file list as still-outstanding work.
function describe(violations, limit = 3) {
  const paths = Object.keys(violations).sort();
  if (paths.length === 0) return null;
  return (
    paths
      .slice(0, 6)
      .map((p) => `${p}: ${violations[p].slice(0, limit).join(", ")}`)
      .join("\n  - ") + (paths.length > 6 ? `\n  - and ${paths.length - 6} more file(s)` : "")
  );
}

const testidNamed = describe(result.non_testid_locators || {});
const navWaitNamed = describe(result.flaky_navigation_waits || {});
if (!testidNamed && !navWaitNamed) process.exit(0);

let message = "";
if (testidNamed) {
  message +=
    "These e2e spec(s) locate elements by role/text/label/tag instead of data-testid -- close this " +
    "now, in this same turn, before finishing. A generic locator can silently match a " +
    "framework-injected element instead of the real one (a Next.js Server Action's own hidden " +
    "<input name=\"$ACTION_ID_...\"> is the live incident this rule exists for); use " +
    "page.getByTestId('...') only, adding a data-testid to the real element if it doesn't have " +
    "one yet:\n" +
    `  - ${testidNamed}\n`;
}
if (navWaitNamed) {
  message +=
    "These e2e spec(s) use waitForNavigation() or a networkidle wait condition -- close this now, " +
    "in this same turn, before finishing. waitForNavigation() can resolve on the wrong hop of a " +
    "multi-hop redirect and networkidle is not guaranteed to ever fire (a shared signIn() helper " +
    "racing an auth redirect regressed a whole e2e suite this way -- 67/74 passing to 30/75); " +
    "replace with a locator-based assertion instead, e.g. " +
    "expect(page.getByTestId('...')).toBeVisible(), which auto-retries regardless of how many " +
    "redirects happen first:\n" +
    `  - ${navWaitNamed}\n`;
}
process.stderr.write(message);
process.exit(2);
