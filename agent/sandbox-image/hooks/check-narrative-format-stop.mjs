#!/usr/bin/env node
// Stop hook: catches a User Story narrative that doesn't match the required "As a <role>, I want
// <capability>, so that <benefit>" template before the specification draft/audit turn even ends,
// instead of waiting a full draft->audit->verify round-trip for spec_ledger.py's
// `check_narrative_format` to report the identical thing.
//
// Root-caused 2026-09-17 (income-investor run 1352296c) and observed AGAIN twice on 2026-09-19
// (income-investor sessions 6244ef47 and 5905ba13, both re-verification runs of an unrelated fix):
// both specification prompts already state this template verbatim as a hard rule, yet a narrative
// missing the "so that" connective, or naming a non-stakeholder role ("the system", "the
// algorithm"), reliably slips through drafting and costs a full redraft lap once the deterministic
// verify gate catches it. This is a fully mechanical, zero-judgment rule (spec_ledger.py's own
// docstring on `check_narrative_format`) -- an ideal same-turn nudge.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT (2026-09-29): this used to hand-port spec_ledger.py's
// narrative-template regex, non-stakeholder-role deny-list, and check function into JS verbatim,
// with no automated drift guard -- "KEEP THESE TWO IN SYNC BY HAND". That logic now lives in
// gates/narrative_format_checks.py (extracted from spec_ledger.py, which imports it back
// unchanged), COPY'd in below UNMODIFIED and stdlib-only, so this hook shells out to the REAL
// implementation instead of a second, independently-drifting copy. See that module's own
// docstring for the full reasoning.
//
// STAGE-SCOPED VIA AIDW_STAGE (final-review fix, 2026-09-30): this comment used to claim no
// AIDW_-prefixed env var gates this hook, relying solely on draft-specification.json's existence
// in the working directory -- that file is never deleted once specification is approved, so this
// hook kept firing on every LATER stage's Stop event for the rest of the run. This is in fact the
// ORIGINAL instance of that bug (check-ledger-sync-stop.mjs's own header cited this file's old
// posture as its precedent and inherited the same gap). Now gated on AIDW_STAGE below, matching
// check-citation-drop-stop.mjs's already-correct pattern.
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-narrative-format-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "specification" && stage !== "brownfield-spec") process.exit(0);

const DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json";

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

const draftSpecPath = `${cwd}/${DRAFT_SPEC_PATH}`;
if (!existsSync(draftSpecPath)) process.exit(0); // not this stage's turn, or file not written yet

let draft;
try {
  draft = JSON.parse(readFileSync(draftSpecPath, "utf8"));
} catch {
  // Present but unreadable/invalid JSON -- a genuine fail-open, unlike the routine absence above.
  reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${DRAFT_SPEC_PATH}`, cwd);
  process.exit(0);
}

if (!Array.isArray(draft.user_stories)) process.exit(0); // not this stage's turn, or file not written yet

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/narrative_format_checks.py", "--check-hook"], {
    input: JSON.stringify({ user_stories: draft.user_stories }),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, "narrative_format_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
  result = JSON.parse(proc.stdout);
} catch {
  reportFailOpen(HOOK_NAME, stage, "unparsable narrative_format_checks.py output", cwd);
  process.exit(0);
}

const violations = result.violations || [];
if (violations.length === 0) process.exit(0);

process.stderr.write(
  "Some User Story narratives don't match the required template ('As a <role>, I want " +
    "<capability>, so that <benefit>'; the role must be a real person/organization, never the " +
    "system itself) -- fix them now, in this same turn, before finishing:\n" +
    violations.map((v) => `- ${v}`).join("\n") +
    "\n",
);
process.exit(2);
