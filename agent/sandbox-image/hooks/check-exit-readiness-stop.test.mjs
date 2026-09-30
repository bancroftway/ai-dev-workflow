// Confirms check-exit-readiness-stop.mjs's own field-access assumptions
// (parsed.readiness / parsed.report.merge_ready / parsed.report.blocking_reasons /
// parsed.report.risk_notes) against lib/read-final-json.mjs's REAL output shape, using a fixture
// transcript in the same style check-adversarial-stop.mjs's own header describes for that helper.
//
// This is deliberately NOT a test of extractFinalJson's own parsing mechanism (that helper is
// already Task 10's proven, shared implementation -- see this hook's own header comment: "reuse,
// don't rebuild"). It only proves ExitDraftResponse/MergeReadinessReport's shape (schemas_exit.py)
// round-trips through the transcript-JSON extraction the same way check-adversarial-stop.mjs's own
// AdversarialAuditDraftResponse already does.
//
// Run: `node --test agent/sandbox-image/hooks/check-exit-readiness-stop.test.mjs` (node's built-in
// test runner -- no new dependency, same as every other stdlib-only piece of this hook family).
import { test } from "node:test";
import assert from "node:assert/strict";
import { extractFinalJson } from "./lib/read-final-json.mjs";

// One JSONL line per transcript entry, mirroring the REAL shape check-full-read-stop.mjs/
// read-final-json.mjs already parse: `{"type": "assistant", "message": {"content": [...]}}`.
function fixtureTranscript(reportObj, readiness = true) {
  const finalMessage = {
    type: "assistant",
    message: { content: [{ type: "text", text: JSON.stringify({ readiness, report: reportObj, skills_invoked: [] }) }] },
  };
  // A leading tool_use line, same as a real turn that read files before its final answer -- proves
  // extractFinalJson correctly skips non-final, non-text blocks rather than grabbing the first line.
  const toolUseLine = { type: "assistant", message: { content: [{ type: "tool_use", name: "Read", input: { file_path: "x" } }] } };
  return [JSON.stringify(toolUseLine), JSON.stringify(finalMessage)].join("\n");
}

test("parsed.readiness / parsed.report round-trip a clean MergeReadinessReport", () => {
  const report = {
    merge_ready: true,
    blocking_reasons: { status: "absent", values: [], reason: "all deterministic checks passed" },
    pr_title: "Add password reset via email",
    pr_description_markdown: "Implements US-0001.",
    risk_notes: { status: "absent", values: [], reason: "no open risks" },
    suggested_reviewers_note: "",
  };
  const parsed = extractFinalJson(fixtureTranscript(report));

  assert.equal(parsed.readiness, true);
  assert.equal(parsed.report.merge_ready, true);
  assert.deepEqual(parsed.report.blocking_reasons, { status: "absent", values: [], reason: "all deterministic checks passed" });
  assert.deepEqual(parsed.report.risk_notes, { status: "absent", values: [], reason: "no open risks" });
});

test("parsed.report.blocking_reasons carries a stale, present blocker exactly as drafted", () => {
  const report = {
    merge_ready: false,
    blocking_reasons: { status: "present", values: ["coverage below threshold"], reason: "" },
    pr_title: "x", pr_description_markdown: "x",
    risk_notes: { status: "absent", values: [], reason: "no open risks" },
  };
  const parsed = extractFinalJson(fixtureTranscript(report));

  assert.equal(parsed.report.merge_ready, false);
  assert.deepEqual(parsed.report.blocking_reasons.values, ["coverage below threshold"]);
});

test("readiness=false (clarifying question) yields no report -- the hook must no-op on this, never nudge", () => {
  const parsed = extractFinalJson(fixtureTranscript(null, false));

  assert.equal(parsed.readiness, false);
  assert.equal(parsed.report, null);
});

test("no assistant text at all (only tool_use) -- extractFinalJson returns null, hook must no-op", () => {
  const toolOnly = JSON.stringify({ type: "assistant", message: { content: [{ type: "tool_use", name: "Read", input: {} }] } });
  assert.equal(extractFinalJson(toolOnly), null);
});
