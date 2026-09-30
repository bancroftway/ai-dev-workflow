#!/usr/bin/env node
// Stop hook: reminds the plan draft/audit turn to explicitly review every `er`/`architecture`
// diagram whose specification appears to have moved on since the diagram was last touched,
// before the turn ends -- instead of waiting a full draft->audit->verify round-trip for
// gates/diagram_gate.py's `check_stale_visual_review` to report the identical gap.
//
// Root-caused 2026-09-19 (income-investor session 5905ba13, run 73bc09ec, plan lap 2): "diagram
// 'data-model' was not reviewed even though the specification changed this ticket" cost a full
// redraft lap.
//
// HONESTLY SCOPED, NOT A FULL PORT -- read this before changing the trigger condition. The real
// gate's own trigger (`_spec_changed_this_run` in gates/diagram_gate.py) is "does any live US/AC
// ledger entry carry THIS run_id in first_seen_run_id/last_revised_run_id" -- but those stamps are
// written by `sync_ledger` only AFTER a passing verify, which happens AFTER this Stop hook's own
// turn already ended. That signal is structurally unavailable at Stop time: not a bug to fix, a
// genuine temporal ordering the real gate can rely on and this same-turn hook cannot. The real
// gate's OTHER half -- whether a diagram already appears in `diagrams_reviewed` -- lives only in
// the model's own final structured response, which this codebase has never confirmed the raw
// on-disk transcript shape of for a non-tool-call, `--json-schema`-constrained answer (unlike the
// Read/Skill tool_use blocks check-full-read-stop.mjs and require-skills-stop.mjs parse, both
// confirmed against real captured samples -- see their own docstrings). Guessing at that shape
// here risks either total inertness or, worse, mis-parsing into a false "not reviewed" claim.
//
// This hook therefore uses a DIFFERENT, purely git-based proxy that needs neither: for each
// `er`/`architecture` diagram, compare the specification's own last commit against the diagram's
// own last commit BY ANCESTRY (`git merge-base --is-ancestor`), not wall-clock timestamp -- two
// commits landing in the same second (routine in a fast-moving sandbox) would otherwise compare
// equal and silently miss a real staleness case. A diagram whose own last commit is an ancestor
// of (or identical to) the specification's own last commit predates whatever that approval last
// changed -- worth an explicit look. This is deliberately UNCONDITIONAL when the file-recency
// signal is true (it cannot see whether the model already planned to report it reviewed), so it
// reads as a checklist reminder ("confirm or revise, then record it"), not an accusation that
// something was skipped --
// same tolerance for a redundant-but-harmless reminder this whole image's hooks already accept
// (check-citation-drop-stop.mjs's own SUSPICIOUS_UNCITED_COUNT threshold exists for the identical
// reason: cheap same-turn nudges are allowed to be imprecise, the deterministic gate stays exact).
//
// Task 12 (2026-09-30) ADDED the PER-ITEM (user_flow diagram/wireframe) half below, using AIDW_
// RUN_ID (Task 5) -- previously graph-side only because a Stop hook had no way to know what
// run_id string to compare the ledger's own first_seen_run_id/last_revised_run_id stamps against.
// That specific blocker is what Task 5 resolves; it does NOT resolve the OTHER, separate blocker
// documented above (diagrams_reviewed/wireframes_reviewed living only in the model's own final
// structured response) -- so the per-item half below still cannot check that field directly, any
// more than the blanket half above can. It reuses this file's OWN existing git-ancestry proxy
// instead (lastCommitHash/isAtOrBefore, already established above), now precisely SCOPED to
// user_flow diagrams/wireframes whose own ac_ids intersect the real (not proxied) trigger set --
// `reopened_or_changed_ac_ids`, shelled out to gates/wireframe_linkage_checks.py (byte-identical
// staged copy at /opt/aidw-hooks/wireframe_linkage_checks.py) -- rather than inventing a second,
// new transcript-parsing mechanism.
//
// STAGE-SCOPED VIA AIDW_STAGE (final-review fix, 2026-09-30): this used to rely solely on
// manifest.json's own existence (which persists for the whole run, read by every later stage) --
// this hook kept firing on every LATER stage's Stop event too. Now gated on AIDW_STAGE below,
// matching check-citation-drop-stop.mjs's already-correct pattern.
// AIDW_RUN_ID is read (Task 5) but only gates the per-item half below; its absence never disables
// the blanket half above.
import { readFileSync, existsSync } from "node:fs";
import { execFileSync, spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-diagram-staleness-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "plan" && stage !== "brownfield-plan") process.exit(0);

const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";
const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const DRAFT_DIAGRAMS_DIR = ".ai-dev-workflow/plan/_draft/diagrams";
const DRAFT_WIREFRAMES_DIR = ".ai-dev-workflow/plan/_draft/wireframes";

/** The hash of the most recent commit touching `relPath`, or null if it has never been committed
 * (a brand-new file this ticket, or git itself unavailable/failed). Never throws. */
function lastCommitHash(cwd, relPath) {
  try {
    const out = execFileSync("git", ["log", "-1", "--format=%H", "--", relPath], {
      cwd,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    return out || null;
  } catch {
    // git itself failed (missing binary, not a repo, etc.) -- distinct from the ordinary
    // "never committed yet" case above, which returns null without ever reaching here.
    reportFailOpen(HOOK_NAME, stage, `git log failed for ${relPath}`, cwd);
    return null;
  }
}

/** True if `commit` is `atOrBefore` in history (an ancestor of it, or the same commit) --
 * ancestry, not wall-clock timestamp, so two commits landing in the same second (routine in a
 * fast-moving sandbox) still compare correctly. Never throws. */
function isAtOrBefore(cwd, commit, atOrBefore) {
  if (commit === atOrBefore) return true;
  try {
    execFileSync("git", ["merge-base", "--is-ancestor", commit, atOrBefore], {
      cwd,
      stdio: ["ignore", "ignore", "ignore"],
    });
    return true; // exit 0 -- commit IS an ancestor of atOrBefore
  } catch {
    return false; // non-zero exit (not an ancestor) or git itself failed -- either way, not stale
  }
}

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

/** Task 12's own addition -- same "not this stage's turn" (undefined) vs "present but unreadable"
 * (fail-open) contract as check-plan-citations-stop.mjs's own `readJson`. */
function readJson(relPath) {
  const path = `${cwd}/${relPath}`;
  if (!existsSync(path)) return undefined;
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${relPath}`, cwd);
    return undefined;
  }
}

const manifestPath = `${cwd}/${MANIFEST_PATH}`;
if (!existsSync(manifestPath)) process.exit(0); // not this stage's turn, or file not written yet

let manifestDoc;
try {
  manifestDoc = JSON.parse(readFileSync(manifestPath, "utf8"));
} catch {
  // Present but unreadable/invalid JSON -- a genuine fail-open, unlike the routine absence above.
  reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${MANIFEST_PATH}`, cwd);
  process.exit(0);
}

const diagrams = Array.isArray(manifestDoc?.diagrams) ? manifestDoc.diagrams : [];
const wireframes = Array.isArray(manifestDoc?.wireframes) ? manifestDoc.wireframes : [];
const staleTargets = diagrams.filter((d) => d?.kind === "er" || d?.kind === "architecture");

const specCommit = lastCommitHash(cwd, SPECIFICATION_APPROVED_PATH);

const staleNames = [];
if (staleTargets.length > 0 && specCommit !== null) {
  for (const d of staleTargets) {
    if (typeof d.name !== "string") continue; // malformed entry -- the schema hook's problem to report, not this one's
    const diagramCommit = lastCommitHash(cwd, `${DRAFT_DIAGRAMS_DIR}/${d.name}.mmd`);
    if (diagramCommit === null) continue; // brand new this ticket -- can't be stale by definition
    if (isAtOrBefore(cwd, diagramCommit, specCommit)) staleNames.push(d.name);
  }
}

// Task 12's own PER-ITEM half (user_flow diagrams + wireframes) -- see this file's own header for
// why it reuses the SAME git-ancestry proxy above rather than checking diagrams_reviewed/
// wireframes_reviewed directly. Gated on AIDW_RUN_ID actually being set: without a real run_id,
// `reopened_or_changed_ac_ids` has nothing meaningful to compare the ledger's own
// first_seen_run_id/last_revised_run_id stamps against.
const staleWireframeScreens = [];
const runId = process.env.AIDW_RUN_ID || null;
if (runId && specCommit !== null) {
  const ledgerDoc = readJson(LEDGER_PATH);
  const ledgerEntries = Array.isArray(ledgerDoc?.entries) ? ledgerDoc.entries : [];
  const specDoc = readJson(SPECIFICATION_APPROVED_PATH);
  const bugAffectedAcIds = Array.isArray(specDoc?.bug_affected_ac_ids) ? specDoc.bug_affected_ac_ids : [];

  let triggerAcIds = [];
  try {
    const proc = spawnSync(
      "python3",
      ["/opt/aidw-hooks/wireframe_linkage_checks.py", "--check-hook"],
      {
        input: JSON.stringify({ ledger_entries: ledgerEntries, run_id: runId, bug_affected_ac_ids: bugAffectedAcIds }),
        encoding: "utf8",
        timeout: 20000,
      },
    );
    if (proc.status === 0 && proc.stdout) {
      triggerAcIds = JSON.parse(proc.stdout).reopened_or_changed_ac_ids || [];
    } else {
      reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or produced no output", cwd);
    }
  } catch {
    reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or returned unparsable output", cwd);
  }
  const triggerSet = new Set(triggerAcIds);

  if (triggerSet.size > 0) {
    const userFlowDiagrams = diagrams.filter((d) => d?.kind === "user_flow");
    for (const d of userFlowDiagrams) {
      if (typeof d.name !== "string" || staleNames.includes(d.name)) continue;
      if (!(d.ac_ids || []).some((a) => triggerSet.has(a))) continue;
      const diagramCommit = lastCommitHash(cwd, `${DRAFT_DIAGRAMS_DIR}/${d.name}.mmd`);
      if (diagramCommit === null) continue; // brand new this ticket -- can't be stale by definition
      if (isAtOrBefore(cwd, diagramCommit, specCommit)) staleNames.push(d.name);
    }
    for (const wf of wireframes) {
      if (typeof wf?.screen !== "string") continue;
      if (!(wf.ac_ids || []).some((a) => triggerSet.has(a))) continue;
      const wireframeCommit = lastCommitHash(cwd, `${DRAFT_WIREFRAMES_DIR}/${wf.screen}.html`);
      if (wireframeCommit === null) continue; // brand new this ticket -- can't be stale by definition
      if (isAtOrBefore(cwd, wireframeCommit, specCommit)) staleWireframeScreens.push(wf.screen);
    }
  }
}

if (staleNames.length === 0 && staleWireframeScreens.length === 0) process.exit(0);

const lines = [];
if (staleNames.length > 0) {
  lines.push(
    ...staleNames.map((n) => `- diagram ${n}`),
  );
}
if (staleWireframeScreens.length > 0) {
  lines.push(...staleWireframeScreens.map((s) => `- wireframe ${s}`));
}
process.stderr.write(
  "The approved specification was committed more recently than these diagram(s)/wireframe(s) (or " +
    "they cite a criterion that changed this run) -- confirm each one is still accurate or revise " +
    "it, and record your decision (action: 'revised' or 'confirmed_current') in " +
    "diagrams_reviewed/wireframes_reviewed before finishing this turn:\n" +
    lines.join("\n") +
    "\n",
);
process.exit(2);
