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
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as this image's other file-only
// hooks: no AIDW_-prefixed env var gates this -- manifest.json's own existence, and git itself
// being available (this pipeline's whole model is a git checkout), is the entire scope check.
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const SPECIFICATION_APPROVED_PATH = ".ai-dev-workflow/03-specification.approved.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const DRAFT_DIAGRAMS_DIR = ".ai-dev-workflow/plan/_draft/diagrams";

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
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

let manifestDoc;
try {
  manifestDoc = JSON.parse(readFileSync(`${cwd}/${MANIFEST_PATH}`, "utf8"));
} catch {
  process.exit(0); // not this stage's turn, or file not written yet
}

const diagrams = Array.isArray(manifestDoc?.diagrams) ? manifestDoc.diagrams : [];
const staleTargets = diagrams.filter((d) => d?.kind === "er" || d?.kind === "architecture");
if (staleTargets.length === 0) process.exit(0);

const specCommit = lastCommitHash(cwd, SPECIFICATION_APPROVED_PATH);
if (specCommit === null) process.exit(0); // spec never committed (or git unavailable) -- nothing to compare against

const staleNames = [];
for (const d of staleTargets) {
  if (typeof d.name !== "string") continue; // malformed entry -- the schema hook's problem to report, not this one's
  const diagramCommit = lastCommitHash(cwd, `${DRAFT_DIAGRAMS_DIR}/${d.name}.mmd`);
  if (diagramCommit === null) continue; // brand new this ticket -- can't be stale by definition
  if (isAtOrBefore(cwd, diagramCommit, specCommit)) staleNames.push(d.name);
}

if (staleNames.length === 0) process.exit(0);

process.stderr.write(
  "The approved specification was committed more recently than these er/architecture diagram(s) " +
    "-- confirm each one is still accurate or revise it, and record your decision (action: " +
    "'revised' or 'confirmed_current') in diagrams_reviewed before finishing this turn:\n" +
    staleNames.map((n) => `- ${n}`).join("\n") +
    "\n",
);
process.exit(2);
