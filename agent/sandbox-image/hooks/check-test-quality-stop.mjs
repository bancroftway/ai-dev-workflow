#!/usr/bin/env node
// Stop hook: catches fiat-failure stubs, absence-only tests, and near-duplicate test bodies in the
// SAME turn that wrote them, instead of waiting a full draft->audit->verify round-trip for
// gates/ac_coverage_gate.py's deterministic_verify (verify_ac_to_tests) to report the same thing.
//
// Root-caused 2026-09-19 (income-investor session f0fef8ba, ac-to-tests cycle 3): the audit turn
// found 0 findings of its own, yet the deterministic gate still rejected on a near-duplicate pair
// the audit never looked for -- US-0013.4's boundary-accepts test and its reject-above-100% sibling
// shared the same arrange/act/assert shape. The gate is right to reject that (a model asked to
// judge its own test suite for redundancy under time pressure reliably misses it), but paying for
// that with a full extra lap is exactly what this file exists to avoid.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: the near-duplicate/absence-only/fiat-stub logic already
// lives in gates/test_quality_checks.py -- extracted 2026-09-19 from ac_coverage_gate.py
// specifically so this hook could shell out to the REAL implementation instead of a second,
// independently-drifting JavaScript copy (the question that prompted the extraction: "why can't
// the hook invoke the same python script that the verification step uses?"). That module is pure
// stdlib (re/difflib only) and ships to /opt/aidw-hooks/test_quality_checks.py unmodified; this
// hook's only job is finding the test files, handing their contents to it on stdin, and reporting
// what comes back on stdout. See that module's own docstring for the full reasoning.
//
// PROVIDER-AGNOSTIC BY THE SAME MECHANISM AS require-skills-stop.mjs, NOT BY FILE EXISTENCE alone:
// gated on AIDW_STAGE=ac-to-tests (claude_chat_model.py's/copilot_chat_model.py's shared
// `_stage_env_prefix`, set unconditionally for every stage/role on BOTH CLIs -- confirmed identical
// wiring for Claude Code and GitHub Copilot CLI, same as every other AIDW_-gated hook here). Scoped
// deliberately, unlike check-plan-schema-stop.mjs's/check-plan-citations-stop.mjs's file-existence
// gating: ac_coverage_gate's near-duplicate/absence-only/fiat-stub checks are wired into
// deterministic_verify for the ac-to-tests stage ONLY (graph.py's make_verify_node registration),
// so firing this hook on any other stage would be the exact "hook fires outside the one turn its
// own logic is scoped to" bug that check-citation-drop-stop.mjs was fixed for on the same day.
//
// TEST_FILE_LISTING below is ported VERBATIM from ac_coverage_gate.py's own `_TEST_FILE_LISTING`
// (same git-ls-files-plus-grep pipeline, same excluded-directory list, same binary-extension
// denylist, same head-60 cap) -- one physical shell pipeline read off the real constant, not a
// second glob pattern invented here that could silently drift from what the gate itself scans.
// KEEP THIS IN SYNC WITH ac_coverage_gate.py'S _TEST_FILE_LISTING BY HAND if that ever changes --
// no automated drift guard exists for a Python string embedded in a JS file across process
// boundaries the way schemas.py's exported JSON Schemas have one.
import { readFileSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";

if (process.env.AIDW_STAGE !== "ac-to-tests") process.exit(0);

const TEST_FILE_LISTING =
  "git ls-files -co --exclude-standard | grep -iE '(test|spec)' " +
  "| grep -viE '(^|/)(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|test-?results|coverage|\\.ai-dev-workflow|agent-work)/' " +
  "| grep -viE '\\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|mp4|webm|woff2?|ttf|eot|dll|exe|so|dylib|pyc|class|jar)$'";

// Matches ac_coverage_gate.py's own `head -60` on this exact listing -- this hook is a same-turn
// NUDGE, not the authoritative gate, so scanning the identical subset the real gate scans means a
// pass here reliably predicts a pass there instead of checking a different (larger) file set.
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
    // race with the model's own in-flight write, or a path `git ls-files` listed that changed
    // between listing and reading -- not this hook's problem to report.
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

const { absence_only: absenceOnly = [], fiat_stubs: fiatStubs = [], duplicates = [] } = result;

const problems = [];

// Named first and directly, same ordering as ac_coverage_gate.py's own problem list: fiat stubs
// are what a model reaches for when it wants RED without work, and letting them fall through to
// the near-duplicate check below produces feedback about similarity the model answers by
// re-wording messages forever instead of writing a real assertion.
if (fiatStubs.length > 0) {
  const named = fiatStubs.slice(0, 3).join("; ") + (fiatStubs.length > 3 ? `; and ${fiatStubs.length - 3} more` : "");
  problems.push(
    `${fiatStubs.length} test(s) fail by fiat (Assert.True(false) / Assert.Fail / ` +
      `expect(true).toBe(false) placeholder bodies): ${named} -- write the real arrange-act-assert ` +
      "against the not-yet-existing API instead; a compile or module-resolution failure is the " +
      "expected RED signal at this stage.",
  );
}

if (absenceOnly.length > 0) {
  const named = absenceOnly.slice(0, 3).join("; ") + (absenceOnly.length > 3 ? `; and ${absenceOnly.length - 3} more` : "");
  problems.push(
    `${absenceOnly.length} test(s) assert ONLY absence (toHaveCount(0), .not.*, ` +
      "expect(...).resolves.toBeUndefined(), Assert.Null/False and friends) with no assertion that " +
      `anything is present: ${named} -- when nothing rendered or nothing was called, every such ` +
      "check is trivially true, so these pass against a blank screen (or a function that does " +
      "nothing) and would pass just as well if the app were entirely broken. Add a positive anchor " +
      "FIRST, matched to what the test drives: a browser/component test anchors on the page " +
      "(`await expect(page.getByTestId('...')).toBeVisible()` / a rendered element), while a unit " +
      "test of an API client/service/store anchors on the observable call or state.",
  );
}

if (duplicates.length > 0) {
  const named = duplicates
    .slice(0, 3)
    .map(([dup, orig]) => `'${dup}' duplicates '${orig}'`)
    .join("; ") + (duplicates.length > 3 ? `; and ${duplicates.length - 3} more` : "");
  problems.push(
    `${duplicates.length} test(s) are near-duplicate bodies of another test (same arrange/act ` +
      `shape, same assertion target): ${named} -- make the named duplicate differ in what it ` +
      "ASSERTS, not how it arranges: assert a different observable (status code, header, " +
      "store/state value, error path), or test the same behavior at a different layer (unit on the " +
      "class + integration over HTTP). Rearranging the same assert is still a duplicate.",
  );
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "The test suite has problems the deterministic ac-to-tests gate will reject at verify time -- " +
    "fix them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
