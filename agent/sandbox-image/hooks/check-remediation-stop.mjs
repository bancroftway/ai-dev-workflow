#!/usr/bin/env node
// Stop hook: runs remediation's own deterministic verdict logic
// (gates/remediation_gate.evaluate_remediation) against THIS turn's own report, for a same-turn
// signal, instead of waiting a full draft->verify round-trip for
// gates/remediation_gate.verify_remediation to report the identical thing.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: evaluate_remediation's verdict logic (unexplained
// actionable findings, fabricated finding ids, scanner-suppression detection) lives in
// gates/remediation_evaluate_checks.py (extracted 2026-09-29, Task 10, from remediation_gate.py,
// which imports it back unchanged -- see that module's own docstring), COPY'd in below UNMODIFIED
// and stdlib-only, so this hook shells out to the REAL implementation instead of a second,
// independently-drifting copy.
//
// THREE INPUTS, THREE SOURCES, NO RE-SCAN:
//   - `scan`: repo-scan-latest.json, already on disk -- remediation_scan_node (the stage's own
//     pre-draft node) publishes it BEFORE the draft turn starts, so it is real by the time this
//     hook runs. This hook does not re-run the scan itself; verify_remediation's own deterministic
//     gate still does that at verify time with the full budget.
//   - `content` (the report itself: findings_addressed/known_gaps/...): like
//     adversarial-compliance, this stage has no on-disk report artifact before the gate runs --
//     content_field=None means the whole structured response IS the report (graph.py's
//     make_draft_node / structured_output.ainvoke_structured: "respond with a single JSON object
//     matching the schema as your FINAL message"). Read via the shared lib/read-final-json.mjs
//     transcript parser, same mechanism check-adversarial-stop.mjs uses and for the same reason.
//   - `changed_files`/`added_lines`/`prior_ids`: gates/remediation_gate.py's own `_stage_diff`/
//     `_prior_finding_ids` compute these via `git diff`/`git show` against this stage's own
//     baseline_commit -- this hook does the identical git calls against AIDW_BASELINE_COMMIT
//     (StageSpec.capture_baseline_commit=True for remediation; claude_chat_model.py's/
//     copilot_chat_model.py's `_baseline_commit_env_prefix` exposes it to this same sandboxed
//     turn, so it is already in this hook's own process.env).
//
// readiness !== true is treated as routine, not a failure -- same reasoning as
// check-adversarial-stop.mjs: RemediationDraftResponse also carries `clarifying_questions`, and a
// not-ready-yet attempt legitimately has an incomplete report.
//
// STAGE-SCOPED VIA AIDW_STAGE == "remediation" (mirrors check-coverage-stop.mjs's own stage gate).
//
// PROVIDER COVERAGE, DISCLOSED: same posture as check-adversarial-stop.mjs's own header -- the
// assistant-message content-blocks array `content` reads from is the SAME structure check-full-
// read-stop.mjs already relies on for Claude (confirmed live there), just filtered for
// `type: "text"` instead of `type: "tool_use"`. No live Copilot transcript sample confirms its own
// final-message shape; registered for both providers anyway because a shape mismatch fails safe
// by construction (no matching text found -> no-op, never a false rejection).
import { readFileSync, existsSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";
import { extractFinalJson } from "./lib/read-final-json.mjs";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-remediation-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "remediation") process.exit(0);

const SCAN_PATH = ".ai-dev-workflow/repo-scan-latest.json";

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
// No parseable final JSON yet, or the model isn't done this attempt -- routine, not a failure.
if (!parsed || parsed.readiness !== true) process.exit(0);

const scanPath = `${cwd}/${SCAN_PATH}`;
if (!existsSync(scanPath)) process.exit(0); // pre-draft scan not written yet -- routine, not a failure

let scan;
try {
  scan = JSON.parse(readFileSync(scanPath, "utf8"));
} catch {
  // Present but unreadable/invalid JSON -- a genuine problem, unlike the routine absence above.
  reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${SCAN_PATH}`, cwd);
  process.exit(0);
}

// changed_files/added_lines/prior_ids: mirrors remediation_gate.py's own _stage_diff/
// _prior_finding_ids exactly, against the SAME baseline_commit those functions use -- absent
// AIDW_BASELINE_COMMIT (never captured, or a stage run before capture_baseline_commit applied)
// degrades to "no diff, no prior scan", the identical fallback both host-side functions already
// return for baseline_commit=None. Shape-validated (a real value is always `git rev-parse HEAD`'s
// own hex SHA) before it reaches an interpolated shell string below -- an env var, however this
// pipeline sets it today, must never be trusted into a shell command unchecked.
const rawBaselineCommit = process.env.AIDW_BASELINE_COMMIT;
const baselineCommit = rawBaselineCommit && /^[0-9a-f]{7,40}$/i.test(rawBaselineCommit) ? rawBaselineCommit : null;

let changedFiles = [];
let addedLines = "";
let priorIds = null;
if (baselineCommit) {
  try {
    changedFiles = execSync(`git diff --name-only ${baselineCommit} -- .`, {
      cwd, encoding: "utf8", shell: "/bin/bash",
    }).split("\n").map((l) => l.trim()).filter(Boolean);
  } catch {
    changedFiles = [];
  }
  try {
    addedLines = execSync(`git diff -U0 ${baselineCommit} -- . | grep '^+' || true`, {
      cwd, encoding: "utf8", shell: "/bin/bash",
    });
  } catch {
    addedLines = "";
  }
  try {
    const raw = execSync(
      `git show ${baselineCommit}:${SCAN_PATH} 2>/dev/null || true`,
      { cwd, encoding: "utf8", shell: "/bin/bash" },
    ).trim();
    if (raw) {
      const prior = JSON.parse(raw);
      priorIds = [...new Set((prior.findings || []).map((f) => String(f.id)))];
    }
  } catch {
    priorIds = null; // unreadable/malformed prior scan -- the fabrication check is skipped, not guessed
  }
}

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/remediation_evaluate_checks.py", "--check-hook"], {
    input: JSON.stringify({
      content: parsed,
      scan,
      changed_files: changedFiles,
      added_lines: addedLines,
      prior_ids: priorIds,
    }),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, "remediation_evaluate_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
  result = JSON.parse(proc.stdout);
} catch {
  reportFailOpen(HOOK_NAME, stage, "unparsable remediation_evaluate_checks.py output", cwd);
  process.exit(0);
}

if (result.passed) process.exit(0);

process.stderr.write(
  "Remediation is not complete -- the deterministic gate will reject this at verify time. Each " +
    "item below is either a finding that is STILL gating/actionable and unexplained, a claimed fix " +
    "that does not correspond to a real finding, or an attempt to silence a scanner. Fix the code, " +
    "or put the finding in `known_gaps` with its real reason, now, in this same turn, before " +
    "finishing:\n" +
    (result.reasons || []).map((r) => `- ${r}`).join("\n") +
    "\n",
);
process.exit(2);
