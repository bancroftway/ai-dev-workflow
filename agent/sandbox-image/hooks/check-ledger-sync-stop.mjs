#!/usr/bin/env node
// Stop hook: catches specification's OWN "dominant rejection mode" (spec_ledger.sync_ledger's
// ~13 existing_us_id/existing_ac_id/retired_us_ids/retired_ac_ids/bug_affected_ac_ids citation-
// validity branches, plus its exact-text citation-drop dedup check) before the whole
// draft->audit->verify round-trip is spent on graph.py's `_verify_specification_ledger` reporting
// the identical thing -- same reasoning as `check-narrative-format-stop.mjs` (Task 8), applied to
// the much larger set of rules that extraction deliberately left uncovered (2026-09-29, Task 11).
//
// A follow-up audit read `_verify_specification_ledger`'s and `sync_ledger`'s FULL bodies (not
// just the narrative-format sub-check Task 8 already covers) and found these ~13 branches are 13
// of this gate's 17 hard rules (see graph.py's own `SPECIFICATION_HARD_RULES` comment) -- until
// now, every one of them had zero Stop-hook equivalent.
//
// WHY A PYTHON SUBPROCESS, NOT A JS PORT: every rule below lives in gates/ledger_sync_checks.py
// (extracted from spec_ledger.py, which imports it back unchanged -- see that module's own
// docstring), COPY'd in below UNMODIFIED and stdlib-only, so this hook shells out to the REAL
// implementation instead of a second, independently-drifting copy.
//
// FOUR THINGS THIS HOOK COVERS, matching this task's own brief:
//   1. Schema-shape validation: `draft-specification.json` against a generated JSON Schema
//      (`schemas.HOOK_SCHEMAS["specification"]` -> `schemas/specification.schema.json`, walked by
//      the SAME generic `lib/json-schema-lite.mjs` plan's schema hook uses). Known gap, not a bug:
//      that walker has no `anyOf` support, so `existing_us_id`/`existing_ac_id` (`str | None`)
//      aren't type-checked here -- graph.py's own `Specification.model_validate` (unchanged) still
//      catches those; this is a same-turn nudge, never the authority.
//   2. Citation/retirement/dedup validity (`gates/ledger_sync_checks.check_ledger_sync_draft`, a
//      validation-only replica of `sync_ledger`'s own citation loop) + the empty-draft rejection +
//      the open-clarifying-question backstop -- fires on EITHER draft or audit turns, gated only
//      by `draft-specification.json` existing (same posture as `check-narrative-format-stop.mjs`).
//   3. The run_id-dependent `fully_reviewed` completeness sweep (`check_fully_reviewed_
//      completeness`) -- audit-turn only, piggybacking `check-full-read-stop.mjs`'s own
//      `AIDW_AUDIT_FULL_READ_FILE` gate for "is this genuinely audit's own turn on the
//      specification stage" (the sandbox has no way to know `fully_reviewed` itself -- that's a
//      transcript-evidence fact computed host-side; this only reports whether `ledger.json`
//      ALREADY shows completeness for `AIDW_RUN_ID`, from an earlier lap of the same run). Reads
//      `AIDW_RUN_ID` (Task 5) -- the ONE sub-check this task's brief calls out as needing it.
//   4. Story decisions (`check_story_decisions`, 2026-10-02): every story in the last-approved
//      specification has one `story_decisions` row whose decision matches what the draft does to
//      it. `specification` stage only; skipped when no approved specification exists yet. Plus
//      `check_prd_changes_addressed`: every `PC-n` change this round's PRD merge declared is tied
//      to a modified/retired story decision or explained in prd_changes_without_story.
//
// STAGE-SCOPED VIA AIDW_STAGE (final-review fix, 2026-09-30): (1)/(2) used to rely solely on
// draft-specification.json's own existence, on the mistaken belief that check-narrative-format-
// stop.mjs's identical posture was safe precedent -- it wasn't (that file had the same bug, from
// which this one inherited it). The file stays on disk for the rest of the ticket, so both used
// to keep firing on every LATER stage's Stop event. Now gated on AIDW_STAGE below, matching
// check-citation-drop-stop.mjs's already-correct pattern. (3) is narrower, as described above.
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { validate } from "./lib/json-schema-lite.mjs";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-ledger-sync-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "specification" && stage !== "brownfield-spec") process.exit(0);

const DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json";
const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
// The last-approved specification: the baseline every live story's `story_decisions` row is judged
// against (ledger_sync_checks.check_story_decisions). Literal value guarded by that module's _demo.
const APPROVED_SPEC_PATH = ".ai-dev-workflow/03-specification.approved.json";
// This round's PRD changes ({changes: [{id: "PC-n", ...}]}), written by the host at every
// specification draft start (spec_ledger.PRD_CHANGES_SCRATCH_PATH; literal guarded the same way).
const PRD_CHANGES_SCRATCH_PATH = ".ai-dev-workflow/spec/prd-changes.json";
// The approved spec as this ticket's round began (spec_ledger.ROUND_BASE_SPEC_PATH; guarded likewise).
const ROUND_BASE_SPEC_PATH = ".ai-dev-workflow/spec/round-base-specification.json";

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

if (typeof draft !== "object" || draft === null) process.exit(0); // not this stage's turn yet

// ledger.json: absence/corruption is the ORDINARY greenfield case (spec_ledger.load_ledger's own
// fail-open contract: no file, or invalid JSON, or a missing/non-array `entries` -> treat as an
// empty ledger), never reported as a hook failure the way an unreadable draft file above is.
let ledgerEntries = [];
const ledgerPath = `${cwd}/${LEDGER_PATH}`;
if (existsSync(ledgerPath)) {
  try {
    const ledgerDoc = JSON.parse(readFileSync(ledgerPath, "utf8"));
    if (Array.isArray(ledgerDoc?.entries)) ledgerEntries = ledgerDoc.entries;
  } catch {
    // Corrupt ledger.json -- spec_ledger.load_ledger's own contract treats this as empty too.
  }
}

const problems = [];

let specSchema;
try {
  specSchema = JSON.parse(readFileSync(new URL("./schemas/specification.schema.json", import.meta.url)));
} catch {
  reportFailOpen(HOOK_NAME, stage, "schema file missing from the image (schemas/specification.schema.json)", cwd);
  specSchema = null; // infra gap -- skip the schema check, never a false rejection; other checks still run
}
if (specSchema) {
  for (const { path, message } of validate(draft, specSchema)) {
    problems.push(`${DRAFT_SPEC_PATH}${path.slice(1)}: ${message}`);
  }
}

// run_id: only trusted when this is genuinely the AUDIT role's own turn on the specification
// stage -- piggybacks check-full-read-stop.mjs's own AIDW_AUDIT_FULL_READ_FILE gate for that exact
// question (set only for the audit role, only when a stage configures the full-read safety net --
// claude_chat_model.py's own `_full_read_env_prefix`/config.AUDIT_FULL_READ_FILE_BY_STAGE) rather
// than inventing a second scoping signal. AIDW_RUN_ID (Task 5) is exposed on EVERY turn, so it
// alone can't tell draft from audit -- the AIDW_AUDIT_FULL_READ_FILE check is what does that.
const runId =
  process.env.AIDW_AUDIT_FULL_READ_FILE === DRAFT_SPEC_PATH && process.env.AIDW_RUN_ID
    ? process.env.AIDW_RUN_ID
    : null;

// story_decisions: the real specification stage only -- brownfield-spec builds the baseline and
// never classifies one, and can run against a non-empty ledger after a lost manifest. The baseline
// is the round-base snapshot (the approved spec as this ticket began), else the approved spec;
// neither readable = no baseline yet, so the check is skipped (null), never guessed at.
function readJsonObject(relPath) {
  const full = `${cwd}/${relPath}`;
  if (!existsSync(full)) return null;
  try {
    const doc = JSON.parse(readFileSync(full, "utf8"));
    return doc && typeof doc === "object" && Object.keys(doc).length > 0 ? doc : null;
  } catch {
    return null;
  }
}
const approvedSpecification =
  stage === "specification" ? readJsonObject(ROUND_BASE_SPEC_PATH) ?? readJsonObject(APPROVED_SPEC_PATH) : null;

// Same stage scoping; absent or unreadable = no PRD changes to account for this round.
let prdChangeIds = [];
const prdChangesPath = `${cwd}/${PRD_CHANGES_SCRATCH_PATH}`;
if (stage === "specification" && existsSync(prdChangesPath)) {
  try {
    const doc = JSON.parse(readFileSync(prdChangesPath, "utf8"));
    if (Array.isArray(doc?.changes)) prdChangeIds = doc.changes.map((c) => c?.id).filter(Boolean);
  } catch {
    prdChangeIds = [];
  }
}

let result;
try {
  const proc = spawnSync("python3", ["/opt/aidw-hooks/ledger_sync_checks.py", "--check-hook"], {
    input: JSON.stringify({
      ledger_entries: ledgerEntries,
      specification: draft,
      run_id: runId,
      approved_specification: approvedSpecification,
      prd_change_ids: prdChangeIds,
    }),
    encoding: "utf8",
    timeout: 20000,
  });
  if (proc.status !== 0 || !proc.stdout) {
    reportFailOpen(HOOK_NAME, stage, "ledger_sync_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
  result = JSON.parse(proc.stdout);
} catch {
  reportFailOpen(HOOK_NAME, stage, "unparsable ledger_sync_checks.py output", cwd);
  process.exit(0);
}

problems.push(...(result.empty_draft_problems || []));
problems.push(...(result.citation_problems || []));
problems.push(...(result.completeness_problems || []));
problems.push(...(result.decision_problems || []));
problems.push(...(result.prd_change_problems || []));

const openQuestions = result.open_questions || [];
if (openQuestions.length > 0) {
  const listed = openQuestions.map((q) => `${q.id}: ${q.question}`).join("; ");
  problems.push(
    "Open clarifying questions can never reach the human gate -- either the revised requirements " +
      "answer them (mark status=answered, citing the wording) or take an explicit assumption " +
      `(status=assumed, mirrored in \`assumptions\`): ${listed}`,
  );
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "The specification draft has problems a deterministic check will reject at verify time -- fix " +
    "them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
