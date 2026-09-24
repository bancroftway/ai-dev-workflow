#!/usr/bin/env node
// Stop hook: runs the fast, low-risk checks (formatting + a narrow SAST slice) in the SAME turn
// the draft wrote the code, instead of waiting for remediation's own separate scan/draft/audit
// round-trip to report the same thing a turn later, against a session that has already moved on
// and lost the context this one still has.
//
// User directive (2026-09-23): "fix a good portion of the SAST/quality/maintainability defects
// earlier, e.g. right at the end of mctg" -- scoped deliberately to formatting (auto-applied,
// zero judgment -- a real diff IS the fix) and a narrow SAST slice (bandit/eslint-security,
// blocked+reported so the model fixes it itself, same posture check-coverage-stop.mjs already
// has for a deterministic gap). Dead-code detection (vulture/ts-prune/knip) is deliberately NOT
// here yet -- no existing tool in repo_scan.py's TOOLS tuple covers it, and auto-removal carries
// real false-positive risk (a dynamically-referenced export) that formatting doesn't. Add it the
// same way this hook was added, once it shows up in practice.
//
// WHY A PYTHON SUBPROCESS FOR THE SAST HALF, NOT A JS PORT: the bandit/eslint-security JSON
// parsing already lives in gates/quick_scan.py -- written 2026-09-23 specifically so this hook
// (and remediation's own full scan, via repo_scan.py's TOOLS tuple importing
// quick_scan.BANDIT_COMMAND/ESLINT_SECURITY_COMMAND directly) share ONE implementation instead of
// a second, independently-drifting JavaScript copy. That module needs nothing beyond the stdlib
// and ships to /opt/aidw-hooks/quick_scan.py unmodified; this hook's only job is running the two
// tools itself, handing their raw JSON to it on stdin, and reporting what comes back on stdout.
// See that module's own docstring for the full reasoning.
//
// PROVIDER-AGNOSTIC BY THE SAME MECHANISM AS check-coverage-stop.mjs: gated on
// AIDW_STAGE=minimal-code-to-green (set unconditionally for every stage/role on both Claude Code
// and Copilot CLI -- see that hook's own comment for the confirmed-identical wiring).
//
// FAILS OPEN, ALWAYS, ON ANYTHING AMBIGUOUS: no Python/JS files to check, a tool that errors or
// times out, output that won't parse -- every one of these means "nothing usable to report", not
// "block the turn". This hook's job is ONLY to catch the common, cheap, unambiguous case (a real,
// fast-to-find defect), never to second-guess or duplicate remediation's own full scan.
import { readFileSync, existsSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";

if (process.env.AIDW_STAGE !== "minimal-code-to-green") process.exit(0);

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

// Per-command ceiling -- this hook can fire on every turn-end attempt, not just once, so a single
// hung tool must not cost minutes every single time. Starting point, not a measured budget (see
// this hook's own PR/task notes for the "verify real wall-clock cost" follow-up) -- a command that
// needs longer than this reliably fails here and simply contributes nothing, same as any other
// infra gap; remediation's own full scan still gets its real budget on its own turn.
const TOOL_TIMEOUT_MS = 60_000;

function runIgnoringFailure(command) {
  try {
    execSync(command, { cwd, shell: "/bin/bash", timeout: TOOL_TIMEOUT_MS, stdio: "ignore" });
  } catch {
    // Fail open -- a formatter/tool exiting non-zero (nothing to format, tool missing, timeout)
    // is not this hook's problem to diagnose.
  }
}

function fileExistsMatching(probeCommand) {
  try {
    execSync(probeCommand, { cwd, shell: "/bin/bash", timeout: 10_000, stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

// --- Formatting: 100% mechanical, auto-applied directly, never blocks --------------------------
// Same applicability probes as repo_scan.py's own _PYTHON_FILES_PROBE/_PACKAGE_JSON_PROBE
// (ToolSpec.applies) -- running a formatter against a tree with nothing of its kind is a harmless
// no-op either way, but skipping it outright avoids spending the per-turn budget on tools with
// nothing to do.
const PYTHON_FILES_PROBE =
  "find . -name '*.py' -not -path '*/node_modules/*' -not -path '*/.venv/*' " +
  "-not -path '*/agent-work/*' -not -path '*/.ai-dev-workflow/*' -print -quit | grep -q .";
const PACKAGE_JSON_PROBE =
  "find . -maxdepth 3 -name package.json -not -path '*/node_modules/*' -print -quit | grep -q .";

if (fileExistsMatching(PYTHON_FILES_PROBE)) {
  runIgnoringFailure("ruff format .");
}
if (fileExistsMatching(PACKAGE_JSON_PROBE) && existsSync(`${cwd}/node_modules/.bin/prettier`)) {
  // --ignore-unknown: a monorepo with mixed file types (Python + TS) must not fail the whole
  // command on a file type prettier has no parser for.
  runIgnoringFailure("node_modules/.bin/prettier --write --ignore-unknown .");
}

// --- SAST: bandit + eslint-security, blocked+reported (needs the model's own judgment) ---------
let banditJson = null;
if (fileExistsMatching(PYTHON_FILES_PROBE)) {
  try {
    execSync("rm -f agent-work/bandit.json", { cwd, shell: "/bin/bash" });
    // Command sourced from quick_scan.py's own BANDIT_COMMAND at hook-write time -- kept as a
    // literal string here (not read from the Python file) since this is a shell command, not
    // JSON; the drift guard is quick_scan.py's own _demo(), which asserts THIS string equals
    // repo_scan.py's live ToolSpec.command every time that self-check runs.
    execSync(
      "bandit -r . -f json -o agent-work/bandit.json -s B101 " +
        "-x './node_modules,./.venv,./apps/*/.venv,./agent-work,./.ai-dev-workflow' --exit-zero",
      { cwd, shell: "/bin/bash", timeout: TOOL_TIMEOUT_MS, stdio: "ignore" },
    );
    banditJson = readFileSync(`${cwd}/agent-work/bandit.json`, "utf8");
  } catch {
    banditJson = null; // tool missing/errored/timed out -- fail open on this tool only
  }
}

let eslintJson = null;
if (fileExistsMatching(PACKAGE_JSON_PROBE) && existsSync("/opt/aidw/lint/node_modules/.bin/eslint")) {
  try {
    execSync("rm -f agent-work/eslint.json", { cwd, shell: "/bin/bash" });
    execSync(
      "/opt/aidw/lint/node_modules/.bin/eslint --no-config-lookup " +
        "--config /opt/aidw/lint/eslint.config.mjs --no-error-on-unmatched-pattern " +
        "-f json -o agent-work/eslint.json . || true",
      { cwd, shell: "/bin/bash", timeout: TOOL_TIMEOUT_MS, stdio: "ignore" },
    );
    eslintJson = readFileSync(`${cwd}/agent-work/eslint.json`, "utf8");
  } catch {
    eslintJson = null;
  }
}

if (banditJson === null && eslintJson === null) process.exit(0); // nothing ran -- nothing to report

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/quick_scan.py", "--check-hook"], {
    input: JSON.stringify({ bandit_json: banditJson, eslint_json: eslintJson }),
    encoding: "utf8",
    timeout: 20_000,
  });
  if (proc.status !== 0 || !proc.stdout) process.exit(0); // infra gap -- never a false rejection
  result = JSON.parse(proc.stdout);
} catch {
  process.exit(0);
}

const findings = result.findings || [];
if (findings.length === 0) process.exit(0);

const named = findings
  .slice(0, 8)
  .map((f) => `${f.file}${f.line ? `:${f.line}` : ""} [${f.tool} ${f.rule_id}, ${f.severity}] ${f.message}`)
  .join("\n  - ") + (findings.length > 8 ? `\n  - and ${findings.length - 8} more` : "");

process.stderr.write(
  `${findings.length} fast SAST finding(s) found in the code you just wrote -- fix them now, in ` +
    "this same turn, instead of leaving them for remediation to catch a stage later:\n" +
    `  - ${named}\n`,
);
process.exit(2);
