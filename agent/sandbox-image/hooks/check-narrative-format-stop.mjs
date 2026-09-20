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
// _NARRATIVE_RE/_NON_STAKEHOLDER_ROLE_WORDS below are PORTED VERBATIM from spec_ledger.py's
// `check_narrative_format` (same regex, same deny-list, same messages) -- not re-derived. Node in
// the sandbox has no path to call Python (see check-plan-schema-stop.mjs's own header for the
// identical constraint). KEEP THESE TWO IN SYNC BY HAND if spec_ledger.py's ever change; there is
// no automated drift guard for this one (unlike agent/src/schemas.py's exported JSON Schemas).
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as check-citation-drop-stop.mjs: no
// AIDW_-prefixed env var gates this -- draft-specification.json's own existence in the working
// directory is the entire scope check.
import { readFileSync } from "node:fs";

const DRAFT_SPEC_PATH = ".ai-dev-workflow/spec/draft-specification.json";

// Ported from spec_ledger.py's `_NARRATIVE_RE`: non-greedy up to the FIRST ", I want" so a
// narrative that happens to contain a second comma inside its capability clause still matches;
// DOTALL because nothing guarantees a model never embeds a stray newline in a narrative string.
const NARRATIVE_RE = /^As (?:a|an|the) (.+?), I want .+, so that .+$/is;

// Ported from spec_ledger.py's `_NON_STAKEHOLDER_ROLE_WORDS` -- same set, same order-independence
// (a plain Set, matching the Python frozenset's semantics exactly).
const NON_STAKEHOLDER_ROLE_WORDS = new Set([
  "system", "application", "api", "backend", "database", "algorithm", "function", "service",
  "platform", "scheduler", "job", "engine", "module", "process",
]);

/** Ported from spec_ledger.py's `_normalize_text`: collapse whitespace, lowercase, trim. */
function normalizeText(text) {
  return text.split(/\s+/).filter(Boolean).join(" ").trim().toLowerCase();
}

/** Ported from spec_ledger.py's `check_narrative_format` -- same two checks, same messages, so a
 * report from this hook reads identically to one from the real deterministic gate. Returns a list
 * of violation strings (empty = all pass). */
function checkNarrativeFormat(userStories) {
  const violations = [];
  for (const story of userStories || []) {
    const narrative = story?.narrative || "";
    const storyId = story?.id || story?.existing_us_id || "(no id)";
    const match = NARRATIVE_RE.exec(narrative);
    if (match === null) {
      violations.push(
        `${storyId}: narrative ${JSON.stringify(narrative)} does not match the required ` +
          "'As a <role>, I want <capability>, so that <benefit>' template (check for a missing " +
          "'that', or a role/capability/benefit segment that isn't actually present).",
      );
      continue;
    }
    let roleText = normalizeText(match[1]);
    if (roleText.startsWith("a ")) roleText = roleText.slice(2);
    else if (roleText.startsWith("an ")) roleText = roleText.slice(3);
    roleText = roleText.trim();
    if (NON_STAKEHOLDER_ROLE_WORDS.has(roleText)) {
      violations.push(
        `${storyId}: narrative ${JSON.stringify(narrative)} names '${match[1]}' as the role, ` +
          "which is not a real human or organizational stakeholder -- never the system itself, a " +
          "module, a function, or a named system component. Name the actual person/role who wants " +
          "this capability instead.",
      );
    }
  }
  return violations;
}

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

let draft;
try {
  draft = JSON.parse(readFileSync(`${cwd}/${DRAFT_SPEC_PATH}`, "utf8"));
} catch {
  process.exit(0); // absent, unreadable, or invalid JSON -- not this hook's problem to report
}

if (!Array.isArray(draft.user_stories)) process.exit(0); // not this stage's turn, or file not written yet

const violations = checkNarrativeFormat(draft.user_stories);
if (violations.length === 0) process.exit(0);

process.stderr.write(
  "Some User Story narratives don't match the required template ('As a <role>, I want " +
    "<capability>, so that <benefit>'; the role must be a real person/organization, never the " +
    "system itself) -- fix them now, in this same turn, before finishing:\n" +
    violations.map((v) => `- ${v}`).join("\n") +
    "\n",
);
process.exit(2);
