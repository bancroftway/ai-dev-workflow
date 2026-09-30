#!/usr/bin/env node
// Stop hook: validates every Acceptance Criterion id `.ai-dev-workflow/plan/_draft/steps.json`
// and `manifest.json` cite before the plan draft/audit turn ends, instead of waiting a full
// draft->audit->verify round-trip for gates/diagram_gate.py to report the identical thing.
//
// Everything below SHELLS OUT to the real Python implementation, gates/wireframe_linkage_checks.py
// (byte-identical staged copy at /opt/aidw-hooks/wireframe_linkage_checks.py) -- not a hand-ported
// reimplementation (2026-09-29, Task 9: the last two hand-ported pieces -- steps'/diagrams' own
// ac_ids citation validity, and retired_step_ids validity -- moved there too, closing the "KEEP IN
// SYNC BY HAND" gap this file used to carry for them). Covers:
// - WIREFRAME/PLAN-STEP <-> AC LINKAGE (citation validity + both coverage directions): every ac_id
//   a wireframe cites is real (`check_wireframe_ac_ids`); every wireframe cites >=1 ac_id
//   (`check_wireframe_has_ac_ids`); every ui_related AC is cited by some wireframe
//   (`check_ui_wireframe_coverage`, root-caused live 2026-09-19, income-investor session 598b633d,
//   plan lap 2); every ui_related plan step is cited by some wireframe
//   (`check_plan_step_wireframe_coverage`, 2026-09-24, the PlanStep-side half of the same coverage
//   discipline).
// - STEP/DIAGRAM AC_IDS CITATION VALIDITY (`check_ac_id_citation`) and RETIRED_STEP_IDS VALIDITY
//   (`check_retired_step_ids`, root-caused live 2026-09-19, income-investor session 5c555dac, plan
//   lap 1: a plan step id retired that was never a real ledger entry, ported from
//   spec_ledger.py's `sync_plan_ledger`).
//
// Deliberately NOT ported at all, still hand-checked below (the "no ac_ids and not
// kind='infrastructure'" structural check) or not checked here at all: the "every eligible AC must
// be cited by SOME step" completeness sweep and the "already-delivered criteria only" carryover
// check (gates/diagram_gate.py's own `check_plan_linkage`) -- both need `coded_run_id`/prior-step
// state this hook would have to re-derive with real risk of getting a subtler rule wrong; the real
// deterministic gate stays the authority for those two.
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as check-citation-drop-stop.mjs and
// check-plan-schema-stop.mjs: no AIDW_-prefixed env var gates this -- steps.json's own existence
// in the working directory is the entire scope check.
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-plan-citations-stop";
const stage = process.env.AIDW_STAGE || "unknown";

const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
const STEPS_PATH = ".ai-dev-workflow/plan/_draft/steps.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

function readJson(relPath) {
  const path = `${cwd}/${relPath}`;
  if (!existsSync(path)) return undefined; // not this stage's turn, or file not written yet
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    // Present but unreadable/invalid JSON -- a genuine fail-open, unlike the routine absence above.
    reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${relPath}`, cwd);
    return undefined;
  }
}

const stepsDoc = readJson(STEPS_PATH);
const manifestDoc = readJson(MANIFEST_PATH);
if (stepsDoc === undefined && manifestDoc === undefined) process.exit(0); // not this stage's turn

const ledgerDoc = readJson(LEDGER_PATH);
const ledgerEntries = Array.isArray(ledgerDoc?.entries) ? ledgerDoc.entries : [];
// Greenfield leniency, same as spec_ledger.sync_ledger's own and check-citation-drop-stop.mjs's:
// an empty ledger means every citation is genuinely unattributable yet, not evidence of a mistake.
// Scoped to the LEDGER-DEPENDENT checks below only (a `problems.length` guard, not `process.exit`)
// -- the ui_related coverage check further down needs only the approved Specification and
// manifest.json, neither of which an empty ledger says anything about; exiting the whole script
// here would have skipped it even on a real, non-greenfield run whose ledger just genuinely has no
// entries yet for some other reason. Root-caused in-session 2026-09-19: this file's own first cut
// exited unconditionally here, which happened to still be correct for every REAL plan turn (the
// ledger is always populated by the time plan drafts -- specification's own verify writes it
// first) but was structurally fragile, and a synthetic test lacking a ledger.json silently proved
// nothing about the coverage check as a result.
const ledgerPopulated = ledgerEntries.length > 0;

const problems = [];

// Structural check ONLY (stays hand-ported -- not an ac_ids VALIDITY question, so out of scope for
// the wireframe_linkage_checks.py extraction): every feature step must cite at least one ac_id
// unless it's kind='infrastructure'. Citation VALIDITY for whatever ac_ids a step DOES cite (bad
// ids, non-live ids) is handled below by the shelled-out `check_ac_id_citation`.
if (ledgerPopulated && Array.isArray(stepsDoc?.plan_steps)) {
  for (const step of stepsDoc.plan_steps) {
    const stepId = step?.id || "?";
    const acIds = Array.isArray(step?.ac_ids) ? step.ac_ids : [];
    if (acIds.length === 0 && step?.kind !== "infrastructure") {
      problems.push(`${stepId}: cites no acceptance criteria and is not kind='infrastructure' -- every feature step must name the US-####.# ids it fulfils.`);
    }
  }
}

// Wireframe/PlanStep <-> AC linkage, step/diagram ac_ids citation validity, and retired_step_ids
// validity: ALL SHELL OUT to gates/wireframe_linkage_checks.py (see this file's own header) instead
// of hand-porting `check_wireframe_ac_ids`/`check_wireframe_has_ac_ids`/`check_ui_wireframe_coverage`/
// `check_plan_step_wireframe_coverage`/`check_ac_id_citation`/`check_retired_step_ids`. Requires the
// approved Specification for the ui_related coverage direction only -- absent for plan's very first
// lap of a first-ever ticket only in the sense that plan cannot start before specification is
// approved, so this file always exists by the time plan's own draft/audit turn runs; still read
// defensively (undefined -> an empty ui_related_ac_ids list, same fail-open discipline as
// everywhere else in this hook).
const specDoc = readJson(SPECIFICATION_APPROVED_PATH);
const uiRelatedAcIds = [];
if (specDoc !== undefined && Array.isArray(specDoc.user_stories)) {
  for (const story of specDoc.user_stories) {
    if (story?.deferred) continue;
    for (const ac of Array.isArray(story?.acceptance_criteria) ? story.acceptance_criteria : []) {
      if (ac?.ui_related && !ac?.deferred && typeof ac?.id === "string") uiRelatedAcIds.push(ac.id);
    }
  }
}
const wireframesForLinkageCheck = Array.isArray(manifestDoc?.wireframes) ? manifestDoc.wireframes : [];
const planStepsForLinkageCheck = Array.isArray(stepsDoc?.plan_steps) ? stepsDoc.plan_steps : [];
const diagramsForCitationCheck = Array.isArray(manifestDoc?.diagrams) ? manifestDoc.diagrams : [];
const retiredStepIdsForCitationCheck = Array.isArray(stepsDoc?.retired_step_ids) ? stepsDoc.retired_step_ids : [];
let checkResult;
try {
  const proc = spawnSync(
    "python3",
    ["/opt/aidw-hooks/wireframe_linkage_checks.py", "--check-hook"],
    {
      input: JSON.stringify({
        wireframes: wireframesForLinkageCheck,
        ledger_entries: ledgerEntries,
        ui_related_ac_ids: uiRelatedAcIds,
        plan_steps: planStepsForLinkageCheck,
        diagrams: diagramsForCitationCheck,
        retired_step_ids: retiredStepIdsForCitationCheck,
      }),
      encoding: "utf8",
      timeout: 20000,
    },
  );
  if (proc.status === 0 && proc.stdout) {
    checkResult = JSON.parse(proc.stdout);
  } else {
    reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or produced no output", cwd);
  }
} catch {
  checkResult = undefined; // infra gap -- never a false rejection
  reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or returned unparsable output", cwd);
}
if (checkResult) {
  // Citation-validity against the ledger only means something once the ledger is populated --
  // same greenfield leniency this hook always applied by hand before extraction. The linkage
  // checks' other three outputs don't depend on ledger state at all (has_ac_ids is a shape check;
  // both coverage directions depend on the Specification/plan_steps, not the ledger), so they run
  // unconditionally.
  if (ledgerPopulated) {
    problems.push(...(checkResult.wireframe_ac_ids || []));
    problems.push(...(checkResult.step_ac_id_problems || []));
    problems.push(...(checkResult.diagram_ac_id_problems || []));
    problems.push(...(checkResult.retired_step_id_problems || []));
  }
  problems.push(...(checkResult.wireframe_has_ac_ids || []));
  problems.push(...(checkResult.ui_wireframe_coverage || []));
  problems.push(...(checkResult.plan_step_wireframe_coverage || []));
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "The plan's own Acceptance Criterion citations have problems a deterministic check will reject " +
    "at verify time -- fix them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
