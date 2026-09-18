#!/usr/bin/env node
// Stop hook: catches a MASS citation-drop (existing_us_id/existing_ac_id reset to null on a large
// batch of already-tracked stories/criteria at once) before the turn even finishes, instead of
// waiting a full draft->audit->verify round-trip for the auditor to notice and fix it.
//
// Root-caused 2026-09-17 (income-investor run 1352296c, lap 3): a redraft that reconstructs the
// specification file from memory rather than genuinely editing it drops EVERY previously-resolved
// citation in one shot (confirmed: all 25 stories/~113 criteria at once) -- the auditor always
// caught and fixed it in the observed data, but that costs a whole wasted lap every time it
// happens. This is a same-turn nudge, not the authoritative check: spec_ledger.py's
// _find_duplicate_by_text (exact-text-match duplicate detection, run at verify time) stays the
// real, precise backstop for whatever this heuristic misses or gets approximately wrong -- this
// script deliberately does NOT do text matching, only counts citations, so it's cheap and has no
// false-positive risk from two genuinely-different stories sharing similar wording.
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION (user directive, 2026-09-17: hooks must work for
// both Claude Code and GitHub Copilot): no AIDW_-prefixed env var gates this at all, unlike
// require-skills-stop.mjs -- the specification files' own existence in the current working
// directory (always /workspace/repo, the repo clone root, for every draft/audit turn on either
// provider) is the entire scope check. A turn for any OTHER stage simply never has
// .ai-dev-workflow/spec/draft-specification.json to find, so this exits 0 immediately regardless
// of which provider or stage is running -- no per-stage/per-role wiring needed anywhere.
import { readFileSync } from "node:fs";

const DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json";
const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";

// Below this many uncited-but-already-tracked ids, this is indistinguishable from an ordinary
// redraft touching a handful of entries -- only fire on the "mass reconstruction" shape the real
// incident showed, not on everyday partial edits (spec_ledger.py's own precise duplicate-text
// check already covers the individual-item case regardless).
const SUSPICIOUS_UNCITED_COUNT = 4;

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as require-skills-stop.mjs /
// .claude/hooks/graph-diagram-check.mjs --stop.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

function readJson(relPath) {
  try {
    return JSON.parse(readFileSync(`${cwd}/${relPath}`, "utf8"));
  } catch {
    return null; // absent, or this turn isn't the specification stage at all -- fail open either way
  }
}

const ledgerDoc = readJson(LEDGER_PATH);
const ledgerEntries = Array.isArray(ledgerDoc?.entries) ? ledgerDoc.entries : [];
// Greenfield leniency, same as spec_ledger.sync_ledger's own: nothing to have dropped a citation
// FROM yet.
const liveIds = new Set(
  ledgerEntries
    .filter((e) => ["active", "revised", "deferred"].includes(e?.status) && (e?.kind === "user_story" || e?.kind === "acceptance_criterion"))
    .map((e) => e.id),
);
if (liveIds.size === 0) process.exit(0);

const draft = readJson(DRAFT_SPEC_PATH);
if (draft === null || !Array.isArray(draft.user_stories)) process.exit(0); // not this stage's turn, or file not written yet

const cited = new Set();
for (const story of draft.user_stories) {
  if (typeof story?.existing_us_id === "string") cited.add(story.existing_us_id);
  for (const ac of Array.isArray(story?.acceptance_criteria) ? story.acceptance_criteria : []) {
    if (typeof ac?.existing_ac_id === "string") cited.add(ac.existing_ac_id);
  }
}

const uncited = [...liveIds].filter((id) => !cited.has(id));
if (uncited.length < SUSPICIOUS_UNCITED_COUNT) process.exit(0);

process.stderr.write(
  `${DRAFT_SPEC_PATH} looks like it was reconstructed from memory rather than edited: ` +
    `${uncited.length} of ${liveIds.size} already-tracked ledger ids (e.g. ${uncited.slice(0, 8).join(", ")}` +
    `${uncited.length > 8 ? ", ..." : ""}) are not cited via existing_us_id/existing_ac_id anywhere ` +
    "in the file. If this content is unchanged from an earlier lap, view the ledger and cite each " +
    "one's real id -- do not regenerate the document from memory, even when many entries need the " +
    "same treatment at once.\n",
);
process.exit(2);
