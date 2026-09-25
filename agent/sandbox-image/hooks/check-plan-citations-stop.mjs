#!/usr/bin/env node
// Stop hook: validates every Acceptance Criterion id `.ai-dev-workflow/plan/_draft/steps.json`
// and `manifest.json` cite before the plan draft/audit turn ends, instead of waiting a full
// draft->audit->verify round-trip for gates/diagram_gate.py to report the identical thing.
//
// Two kinds of check below, same split as check-testid-locators-stop.mjs vs. its own
// hand-ported siblings:
//
// 1. WIREFRAME/PLAN-STEP <-> AC LINKAGE (citation validity + both coverage directions) SHELLS OUT
//    to the real Python implementation, gates/wireframe_linkage_checks.py (byte-identical staged
//    copy at /opt/aidw-hooks/wireframe_linkage_checks.py) -- not a hand-ported reimplementation.
//    Covers: every ac_id a wireframe cites is real (`check_wireframe_ac_ids`); every wireframe
//    cites >=1 ac_id (`check_wireframe_has_ac_ids`); every ui_related AC is cited by some
//    wireframe (`check_ui_wireframe_coverage`, root-caused live 2026-09-19, income-investor
//    session 598b633d, plan lap 2); every ui_related plan step is cited by some wireframe
//    (`check_plan_step_wireframe_coverage`, 2026-09-24, the PlanStep-side half of the same
//    coverage discipline).
// 2. Everything else below stays hand-ported (steps'/diagrams' own ac_ids citation validity, and
//    retired_step_ids validity -- root-caused live 2026-09-19, income-investor session 5c555dac,
//    plan lap 1: a plan step id retired that was never a real ledger entry, ported from
//    spec_ledger.py's `sync_plan_ledger`). Deliberately NOT ported at all: the "every eligible AC
//    must be cited by SOME step" completeness sweep and the "already-delivered criteria only"
//    carryover check (gates/diagram_gate.py's own `check_plan_linkage`) -- both need
//    `coded_run_id`/prior-step state this hook would have to re-derive with real risk of getting a
//    subtler rule wrong; the real deterministic gate stays the authority for those two.
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as check-citation-drop-stop.mjs and
// check-plan-schema-stop.mjs: no AIDW_-prefixed env var gates this -- steps.json's own existence
// in the working directory is the entire scope check.
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
const STEPS_PATH = ".ai-dev-workflow/plan/_draft/steps.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

function readJson(relPath) {
  try {
    return JSON.parse(readFileSync(`${cwd}/${relPath}`, "utf8"));
  } catch {
    return undefined; // absent, unreadable, or invalid JSON -- reported separately (schema hook) or not this stage's turn
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

const byId = new Map(ledgerEntries.map((e) => [e.id, e]));
const LIVE_STATUSES = new Set(["active", "revised"]);

/** Ported from gates/diagram_gate.py's `check_plan_linkage`/`check_wireframe_ac_ids`: an ac_id is
 * valid only if it exists in the ledger, is kind='acceptance_criterion', and (for plan steps,
 * which may only cite currently-live work) is not retired/deferred. Wireframes/diagrams may cite
 * a deferred id too in the real gate's own citation-validity check (only existence+kind, not
 * liveness, matters there) -- `requireLive` threads that distinction through one shared function
 * rather than two near-duplicate copies. */
function checkAcIds(label, acIds, requireLive) {
  const problems = [];
  const bad = acIds.filter((id) => {
    const entry = byId.get(id);
    return entry === undefined || entry.kind !== "acceptance_criterion";
  });
  if (bad.length > 0) {
    problems.push(`${label}: cites ${bad.join(", ")} which is not an acceptance criterion in the ledger -- copy ids exactly from the approved Specification.`);
    return problems; // don't also report liveness for an id that doesn't even exist
  }
  if (requireLive) {
    const nonLive = acIds.filter((id) => !LIVE_STATUSES.has(byId.get(id).status));
    if (nonLive.length > 0) {
      problems.push(
        `${label}: cites ${nonLive.join(", ")}, which ${nonLive.length === 1 ? "is" : "are"} retired or ` +
          "deferred -- ac_ids may only name LIVE criteria; a retired criterion's delivered artifacts " +
          "belong in removes_ids instead, and deferred scope must not be planned at all.",
      );
    }
  }
  return problems;
}

const problems = [];

// retired_step_ids validity, ported from spec_ledger.py's `sync_plan_ledger` (root-caused live
// 2026-09-19, income-investor session 5c555dac, plan lap 1: "retired_step_ids cites 'PS-26', which
// does not exist in the ledger" -- a step id that was never a real ledger entry, retired anyway).
// Every id here must already be a ledger `plan_step` entry, and must not ALSO appear in this
// draft's own plan_steps (revise-or-retire, never both, same discipline as ac_ids' own
// retired-vs-live rule above).
if (ledgerPopulated && Array.isArray(stepsDoc?.retired_step_ids)) {
  const draftStepIds = new Set(
    Array.isArray(stepsDoc.plan_steps) ? stepsDoc.plan_steps.filter((s) => typeof s?.id === "string").map((s) => s.id) : [],
  );
  for (const stepId of stepsDoc.retired_step_ids) {
    if (typeof stepId !== "string") continue;
    const entry = byId.get(stepId);
    if (entry === undefined) {
      problems.push(`retired_step_ids cites ${JSON.stringify(stepId)}, which does not exist in the ledger.`);
    } else if (entry.kind !== "plan_step") {
      problems.push(`retired_step_ids cites ${JSON.stringify(stepId)}, which is not a plan step id.`);
    } else if (draftStepIds.has(stepId)) {
      problems.push(`retired_step_ids cites ${JSON.stringify(stepId)}, but this draft also revises it -- a step cannot be both revised and retired in the same draft.`);
    }
  }
}

if (ledgerPopulated && Array.isArray(stepsDoc?.plan_steps)) {
  for (const step of stepsDoc.plan_steps) {
    const stepId = step?.id || "?";
    const acIds = Array.isArray(step?.ac_ids) ? step.ac_ids : [];
    if (acIds.length === 0) {
      if (step?.kind !== "infrastructure") {
        problems.push(`${stepId}: cites no acceptance criteria and is not kind='infrastructure' -- every feature step must name the US-####.# ids it fulfils.`);
      }
      continue;
    }
    problems.push(...checkAcIds(stepId, acIds, true));
  }
}

if (ledgerPopulated && Array.isArray(manifestDoc?.diagrams)) {
  for (const dg of manifestDoc.diagrams) {
    const acIds = Array.isArray(dg?.ac_ids) ? dg.ac_ids : [];
    if (acIds.length > 0) {
      problems.push(...checkAcIds(`diagram ${JSON.stringify(dg?.name || "?")}`, acIds, false));
    }
  }
}

// Wireframe/PlanStep <-> AC linkage: SHELLS OUT to gates/wireframe_linkage_checks.py (see this
// file's own header) instead of hand-porting `check_wireframe_ac_ids`/`check_wireframe_has_ac_ids`/
// `check_ui_wireframe_coverage`/`check_plan_step_wireframe_coverage`. Requires the approved
// Specification for the ui_related coverage direction only -- absent for plan's very first lap of
// a first-ever ticket only in the sense that plan cannot start before specification is approved,
// so this file always exists by the time plan's own draft/audit turn runs; still read defensively
// (undefined -> an empty ui_related_ac_ids list, same fail-open discipline as everywhere else in
// this hook).
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
let linkageResult;
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
      }),
      encoding: "utf8",
      timeout: 20000,
    },
  );
  if (proc.status === 0 && proc.stdout) linkageResult = JSON.parse(proc.stdout);
} catch {
  linkageResult = undefined; // infra gap -- never a false rejection
}
if (linkageResult) {
  // Citation-validity against the ledger only means something once the ledger is populated --
  // same greenfield leniency as every checkAcIds call above. The other three checks don't depend
  // on ledger state at all (has_ac_ids is a shape check; both coverage directions depend on the
  // Specification/plan_steps, not the ledger), so they run unconditionally.
  if (ledgerPopulated) problems.push(...(linkageResult.wireframe_ac_ids || []));
  problems.push(...(linkageResult.wireframe_has_ac_ids || []));
  problems.push(...(linkageResult.ui_wireframe_coverage || []));
  problems.push(...(linkageResult.plan_step_wireframe_coverage || []));
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "The plan's own Acceptance Criterion citations have problems a deterministic check will reject " +
    "at verify time -- fix them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
