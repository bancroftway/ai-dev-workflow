#!/usr/bin/env node
// Stop hook: runs adversarial-compliance's own deterministic verdict logic
// (gates/adversarial_gate.evaluate_audit) against THIS turn's own report, for a same-turn signal,
// instead of waiting a full draft->verify round-trip for gates/adversarial_gate.verify_
// adversarial_compliance to report the identical thing.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: evaluate_audit's verdict logic (blocking verdicts,
// per-finding critical/major severity) lives in gates/adversarial_audit_checks.py (extracted
// 2026-09-29, Task 10, from adversarial_gate.py, which imports it back unchanged -- see that
// module's own docstring), COPY'd in below UNMODIFIED and stdlib-only, so this hook shells out to
// the REAL implementation instead of a second, independently-drifting copy.
//
// NO ON-DISK REPORT ARTIFACT: unlike e.g. plan's steps.json, this stage's report never touches a
// repo file before the gate runs -- it is the model's own final turn message (graph.py's
// make_draft_node calls structured_output.ainvoke_structured, whose whole contract is "respond
// with a single JSON object matching the schema as your FINAL message"). So this hook reads the
// SAME transcript check-full-read-stop.mjs/require-skills-stop.mjs already rely on
// (`input.transcript_path`, already on disk by the time Stop fires) via the shared
// lib/read-final-json.mjs helper, instead of a file this stage never writes.
//
// readiness !== true (the model is still asking a clarifying question, per
// AdversarialAuditDraftResponse's own `clarifying_questions`) is treated as routine, not a
// failure: `report` is legitimately absent/incomplete on that attempt, and flagging it here would
// be a false rejection of a normal not-ready-yet turn.
//
// STAGE-SCOPED VIA AIDW_STAGE == "adversarial-compliance" (mirrors check-coverage-stop.mjs's own
// stage gate): this hook's transcript-scan is meaningless for any other stage's own final-message
// shape.
//
// PROVIDER COVERAGE, DISCLOSED: the assistant-message content-blocks array this reads is the SAME
// structure check-full-read-stop.mjs already relies on for Claude (confirmed live there), just
// filtered for `type: "text"` instead of `type: "tool_use"` -- not a new, unconfirmed shape. No
// live Copilot transcript sample has confirmed its own final-message shape the way that hook's own
// header discloses for Read tool_use blocks; unlike that hook, this one is registered for both
// providers anyway, because a shape mismatch fails safe by construction here -- extractFinalJson
// simply finds no matching assistant text and this hook no-ops (exit 0), never a false rejection.
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { extractFinalJson } from "./lib/read-final-json.mjs";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-adversarial-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "adversarial-compliance") process.exit(0);

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

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/adversarial_audit_checks.py", "--check-hook"], {
    input: JSON.stringify({ report: parsed.report ?? null }),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, "adversarial_audit_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
  result = JSON.parse(proc.stdout);
} catch {
  reportFailOpen(HOOK_NAME, stage, "unparsable adversarial_audit_checks.py output", cwd);
  process.exit(0);
}

if (result.passed) process.exit(0);

process.stderr.write(
  "The adversarial audit's own report does NOT conform to the approved Plan and Specification -- " +
    "the deterministic gate will reject this at verify time. Fix it now, in this same turn, before " +
    "finishing (or, where the finding is demonstrably wrong about the Plan, say so with evidence " +
    "rather than lowering its severity):\n" +
    (result.reasons || []).map((r) => `- ${r}`).join("\n") +
    "\n",
);
process.exit(2);
