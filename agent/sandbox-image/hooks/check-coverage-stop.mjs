#!/usr/bin/env node
// Stop hook: replays the committed coverage-commands.json contract and checks it against the real
// threshold, THEN (only once coverage passes -- the real gate's own sequencing) checks per-AC test
// DEPTH the same way test_coverage_gate.check_ac_depth does, all in the SAME turn the draft wrote
// the code, instead of waiting a full draft->audit->verify round-trip for
// gates/test_coverage_gate.py's deterministic_verify (verify_coverage) to report the same gap.
//
// Root-caused 2026-09-21 (income-investor session f0fef8ba, minimal-code-to-green): the large
// majority of this stage's redraft cycles were triggered by verify_coverage's contract replay --
// a 100% deterministic, non-LLM check -- finding a line/branch percentage shortfall that the draft
// could have found and fixed itself before ever ending its turn. Each cycle that fails this way
// costs a full fresh audit session (reads the whole diff) plus a fresh draft session (rediscovers
// context the just-ended turn already had), for something this hook can catch inline.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: the artifact-parsing and threshold/merge arithmetic
// already live in gates/coverage_parsing.py -- extracted 2026-09-21 from test_coverage_gate.py
// specifically so this hook (and check-test-quality-stop.mjs's own sibling extraction one day
// earlier) could shell out to the REAL implementation instead of a second, independently-drifting
// JavaScript copy. That module needs only `defusedxml` beyond the stdlib (baked into this image
// via Dockerfile's DEFUSEDXML_VERSION specifically for this hook) and ships to
// /opt/aidw-hooks/coverage_parsing.py unmodified; this hook's only job is replaying the contract's
// own commands, handing the resulting artifacts to it on stdin, and reporting what comes back on
// stdout. See that module's own docstring for the full reasoning.
//
// PROVIDER-AGNOSTIC BY THE SAME MECHANISM AS check-test-quality-stop.mjs: gated on
// AIDW_STAGE=minimal-code-to-green (set unconditionally for every stage/role on both Claude Code
// and Copilot CLI -- see that hook's own comment for the confirmed-identical wiring). Scoped
// deliberately: test_coverage_gate's verify_coverage is wired into deterministic_verify for
// minimal-code-to-green ONLY (graph.py's make_verify_node registration), so firing this hook on
// any other stage would replay a contract that stage's own gate never checks.
//
// FAILS OPEN, ALWAYS, ON ANYTHING AMBIGUOUS: no contract file yet, a malformed contract, a command
// that errors, an artifact that won't parse -- every one of these is a real condition the
// deterministic gate's own re-discovery path already handles correctly (see
// test_coverage_gate.py's `_run_coverage_via_ghcp`), and this hook's job is ONLY to catch the
// common, cheap, unambiguous case (a valid contract whose replay is simply under threshold), never
// to second-guess or duplicate that gate's own infra-failure handling.
import { readFileSync, existsSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";

if (process.env.AIDW_STAGE !== "minimal-code-to-green") process.exit(0);

// Ported VERBATIM from check-test-quality-stop.mjs's own TEST_FILE_LISTING -- same pipeline, same
// cap, so the AC-depth half of this hook scans the identical file set the ac-to-tests hook and the
// real gates scan. See that hook's own comment for the KEEP IN SYNC BY HAND caveat.
const TEST_FILE_LISTING =
  "git ls-files -co --exclude-standard | grep -iE '(test|spec)' " +
  "| grep -viE '(^|/)(node_modules|\\.playwright-browsers|bin|obj|dist|build|\\.next|\\.venv|vendor|test-?results|coverage|\\.ai-dev-workflow|agent-work)/' " +
  "| grep -viE '\\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|tar|mp4|webm|woff2?|ttf|eot|dll|exe|so|dylib|pyc|class|jar)$'";
const TEST_FILE_LISTING_CAP = 60;

const COVERAGE_COMMANDS_PATH =
  process.env.AIDW_COVERAGE_COMMANDS_PATH || ".ai-dev-workflow/coverage-commands.json";
const MIN_COVERAGE_PERCENT = parseFloat(process.env.MIN_COVERAGE_PERCENT || "95.0");
const CONTRACT_FORMATS = new Set(["cobertura", "istanbul-json-summary"]);

// Per-command ceiling for this hook's OWN replay -- deliberately shorter than
// TEST_COVERAGE_REPLAY_TIMEOUT_SECONDS' full 600s default budget (the real gate's own replay):
// this hook can fire on every turn-end attempt, not just once, so a single hung command must not
// cost minutes every single time. A command that needs longer than this reliably fails here and
// falls through to fail-open, same as any other infra gap -- the real gate still gets the full
// budget on its own turn. Kept well under the Stop hook's own 300s timeout (managed-settings.json/
// copilot/config.json) so a two-entry contract (backend + frontend) has headroom to finish even if
// one command runs close to this ceiling.
const REPLAY_TIMEOUT_MS = 120_000;

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";
const contractPath = `${cwd}/${COVERAGE_COMMANDS_PATH}`;
if (!existsSync(contractPath)) process.exit(0); // no contract established yet -- nothing to replay

let contract;
try {
  contract = JSON.parse(readFileSync(contractPath, "utf8"));
} catch {
  process.exit(0); // unreadable/malformed -- the real gate's own loader will report this properly
}

const rawEntries = Array.isArray(contract?.entries) ? contract.entries : [];
const entries = rawEntries.filter(
  (e) =>
    e &&
    typeof e.command === "string" &&
    e.command.trim() &&
    typeof e.artifact === "string" &&
    e.artifact.trim() &&
    !e.artifact.startsWith("/") &&
    !e.artifact.split("/").includes("..") &&
    CONTRACT_FORMATS.has(e.format),
);
// A contract that doesn't validate cleanly is not this hook's problem to diagnose -- same
// fail-open reasoning as every other ambiguous case here.
if (entries.length === 0 || entries.length !== rawEntries.length) process.exit(0);

// execSync + a shell, not execFile, is deliberate and safe here: entry.command is a real shell
// command ("cd apps/web && npx vitest run --coverage"), not expressible as one executable + an
// argv array, and it was authored by THIS SAME agent's own prior turn into its own contract file --
// not external/user input crossing a trust boundary. It runs at the exact same trust level as
// every Bash tool call this agent's own turn already makes in this sandbox, and mirrors
// test_coverage_gate.py's own host-side `_replay_coverage_contract`, which shell-executes this
// identical string via `provider.exec_in_sandbox`.
const replayed = [];
for (const entry of entries) {
  const root = (entry.root || "").trim() || ".";
  const artifactAbs = `${cwd}/${entry.artifact}`;
  try {
    execSync(`rm -f ${JSON.stringify(entry.artifact)}`, { cwd, shell: "/bin/bash" });
    execSync(entry.command, { cwd: `${cwd}/${root}`, shell: "/bin/bash", timeout: REPLAY_TIMEOUT_MS, stdio: "ignore" });
  } catch {
    replayed.push({ format: entry.format, content: null, error: `command failed or timed out: ${entry.command}` });
    continue;
  }
  try {
    replayed.push({ format: entry.format, content: readFileSync(artifactAbs, "utf8") });
  } catch {
    replayed.push({ format: entry.format, content: null, error: `no artifact produced at ${entry.artifact}` });
  }
}

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/coverage_parsing.py", "--check-hook"], {
    input: JSON.stringify({ entries: replayed }),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) process.exit(0); // infra gap -- never a false rejection
  result = JSON.parse(proc.stdout);
} catch {
  process.exit(0);
}

const { line_rate: lineRate, branch_rate: branchRate, gaps = [] } = result;

// Nothing usable parsed at all -- this is "the contract doesn't work right now", not "coverage is
// low"; the real gate's own re-discovery path exists exactly for this and gives a much more
// specific diagnosis than this hook could. Never block on it here.
if (lineRate === null || branchRate === null) process.exit(0);

if (lineRate < MIN_COVERAGE_PERCENT || branchRate < MIN_COVERAGE_PERCENT) {
  const named = gaps
    .slice(0, 8)
    .map((g) => `${g.file} (line ${g.line_rate.toFixed(1)}%, branch ${g.branch_rate.toFixed(1)}%)`)
    .join("; ") + (gaps.length > 8 ? `; and ${gaps.length - 8} more` : "");

  process.stderr.write(
    `Coverage ${lineRate.toFixed(1)}%/${branchRate.toFixed(1)}% (line/branch) is below the ` +
      `${MIN_COVERAGE_PERCENT}% threshold the deterministic gate will enforce at verify time -- ` +
      "close this now, in this same turn, before finishing. Files below threshold: " +
      `${named || "(see full gate output)"}\n`,
  );
  process.exit(2);
}

// Coverage passed -- now check per-AC test DEPTH the same way test_coverage_gate.check_ac_depth
// does right after its own verify_coverage passes (deterministic_verify's own sequencing: depth is
// only checked once the number itself clears the bar). Same fail-open posture as the coverage half
// above: a python/infra gap here is never a false rejection, only a genuinely parsed shortfall is.
let testPaths = [];
try {
  const out = execSync(`(${TEST_FILE_LISTING}) | head -${TEST_FILE_LISTING_CAP}`, {
    cwd,
    encoding: "utf8",
    shell: "/bin/bash",
  });
  testPaths = out.split("\n").map((l) => l.trim()).filter(Boolean);
} catch {
  process.exit(0); // git not available -- fail open
}

if (testPaths.length === 0) process.exit(0); // no test files found -- nothing to check depth on

const testFiles = {};
for (const path of testPaths) {
  try {
    testFiles[path] = readFileSync(`${cwd}/${path}`, "utf8");
  } catch {
    // race with an in-flight write -- not this hook's problem to report.
  }
}
if (Object.keys(testFiles).length === 0) process.exit(0);

let qualityResult;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/test_quality_checks.py", "--check-hook"], {
    input: JSON.stringify(testFiles),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) process.exit(0); // infra gap -- never a false rejection
  qualityResult = JSON.parse(proc.stdout);
} catch {
  process.exit(0);
}

const acDepth = qualityResult.ac_depth || {};
const shortAcIds = Object.keys(acDepth).sort();
if (shortAcIds.length === 0) process.exit(0);

const acLines = shortAcIds
  .slice(0, 6)
  .map((ac) => `${ac}: ${acDepth[ac].shortfalls.join("; ")}`)
  .join("\n  - ") + (shortAcIds.length > 6 ? `\n  - and ${shortAcIds.length - 6} more AC(s)` : "");

process.stderr.write(
  `${shortAcIds.length} acceptance criteria have test-depth shortfalls the deterministic gate ` +
    "will reject at verify time -- close them now, in this same turn, before finishing:\n" +
    `  - ${acLines}\n`,
);
process.exit(2);
