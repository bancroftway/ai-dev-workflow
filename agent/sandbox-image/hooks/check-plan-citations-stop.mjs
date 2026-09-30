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
// Task 12 (2026-09-30) ported the two checks the ABOVE paragraph used to leave un-ported, after
// re-reading (not paraphrasing) that paragraph's own stated reason: "both need coded_run_id/
// prior-step state this hook would have to re-derive with real risk of getting a subtler rule
// wrong" -- a re-derivation/re-implementation-drift concern (the risk was always about this hook
// having to independently RECOMPUTE that state in hand-ported JS, not about the state being stale
// or unavailable at Stop time), which shelling out to the REAL Python function resolves exactly
// the same way check_ac_id_citation/check_retired_step_ids already did above. So, now also ported,
// both via `run_plan_linkage_checks`:
// - COVERAGE-SIDE (`check_plan_step_coverage_and_rework`): every ELIGIBLE ac id (this ticket's own,
//   live, never delivered by a healthy run) must be cited by >=1 step.
// - REWORK-FORBIDDEN (same function): a NEW/CHANGED step (vs the prior approved plan, read from
//   `PLAN_APPROVED_PATH` below) citing only already-delivered criteria is rework the pipeline
//   forbids; verbatim carryovers are exempt.
// Also added (plain misses, not deliberate exclusions -- confirmed pure-on-disk, no run_id needed):
// - `check_dangling_visual_retirement`: a wireframe/user_flow diagram citing only retired ACs
//   without being named retired itself.
// - `check_removes_ids_validity` (+ its own run_id-gated demand half, `check_plan_removal_demand`,
//   now wired via AIDW_RUN_ID/Task 5 -- see the removal side of `check_plan_linkage`'s own
//   docstring in diagram_gate.py): removes_ids existence/retired-status validity.
// - `check_plan_step_ids` (spec_ledger.py's `sync_plan_ledger`): the plan-step id-missing/
//   id-collision guard.
//
// STAGE-SCOPED VIA AIDW_STAGE (final-review fix, 2026-09-30): this used to rely solely on
// steps.json's own existence, which stays on disk for the rest of the ticket once plan is
// approved -- this hook kept firing on every LATER stage's Stop event too. Now gated on
// AIDW_STAGE below, matching check-citation-drop-stop.mjs's already-correct pattern.
// AIDW_RUN_ID is read (Task 5) but only gates the ONE removal-demand sub-check above, never this
// hook's own stage scope.
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-plan-citations-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "plan" && stage !== "brownfield-plan") process.exit(0);

const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
const STEPS_PATH = ".ai-dev-workflow/plan/_draft/steps.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";
const PLAN_APPROVED_PATH = ".ai-dev-workflow/04-plan.approved.json";

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

// Task 12's own additions -- retired_wireframe_screens/retired_diagram_names (already on
// manifestDoc, just not read by this hook until now) for check_dangling_visual_retirement; the
// PRIOR approved plan's own steps (absent on a first-ever plan -- no prior approval exists yet,
// same defensive `undefined` handling as specDoc below) for check_plan_step_coverage_and_rework's
// rework-forbidden carryover comparison; AIDW_RUN_ID (Task 5) for the removal-side demand
// direction only (`run_id: null` skips just that one sub-check, same contract
// diagram_gate.check_plan_linkage's own `run_id=None` already has).
const retiredWireframeScreens = Array.isArray(manifestDoc?.retired_wireframe_screens) ? manifestDoc.retired_wireframe_screens : [];
const retiredDiagramNames = Array.isArray(manifestDoc?.retired_diagram_names) ? manifestDoc.retired_diagram_names : [];
const priorPlanDoc = readJson(PLAN_APPROVED_PATH);
const priorPlanSteps = Array.isArray(priorPlanDoc?.plan_steps) ? priorPlanDoc.plan_steps : [];
const runId = process.env.AIDW_RUN_ID || null;

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
        retired_wireframe_screens: retiredWireframeScreens,
        retired_diagram_names: retiredDiagramNames,
        specification: specDoc || null,
        prior_plan_steps: priorPlanSteps,
        run_id: runId,
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
    // Task 12: all four also need a real ledger to mean anything (retired-AC lookups, coded_run_id
    // delivery stamps, id-collision detection) -- same greenfield leniency as the four above.
    problems.push(...(checkResult.dangling_visual_retirement_problems || []));
    problems.push(...(checkResult.removes_ids_problems || []));
    problems.push(...(checkResult.plan_step_coverage_rework_problems || []));
    problems.push(...(checkResult.plan_step_id_problems || []));
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
