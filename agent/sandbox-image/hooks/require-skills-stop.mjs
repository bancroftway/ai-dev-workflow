#!/usr/bin/env node
// Stop hook: forces a stage's mandatory skills (agent/src/config.py REQUIRED_SKILLS_BY_STAGE) to
// actually be invoked before a draft turn is allowed to finish, instead of only catching the miss
// after the whole lap already failed. Root-caused 2026-09-17: specification_draft.md already tells
// the model "MANDATORY, NOT ADVISORY -- a deterministic gate REJECTS the whole draft if missing",
// worded as strongly as a prompt gets, and a live run still skipped both required skills twice in
// a row. Prompt-only compliance is advisory no matter the wording; this makes it deterministic at
// the point it matters (before the turn ends) rather than only at the graph's redraft gate.
//
// This is a same-turn fast path, not a replacement for gates/skill_gate.py: skill_gate stays the
// fail-closed backstop for whenever this hook is unavailable (Copilot has no hook equivalent),
// misconfigured, or bypassed some other way -- exactly the posture claude_chat_model.py's
// read_skill_invocations/read_full_file_reads already document for the same reason.
//
// Activated per-turn via the AIDW_REQUIRED_SKILLS env var (comma-separated skill/agent names),
// set only on the "draft" role's CLI invocation (claude_chat_model.py) -- every
// REQUIRED_SKILLS_BY_STAGE entry's full list is documented in that stage's OWN *_draft.md prompt
// (confirmed by reading all of them), so gating on draft alone needs no per-role subset logic and
// never falsely blocks an audit session whose own prompt never asked for these skills.
//
// Transcript line shape mirrored exactly from claude_chat_model.read_skill_invocations/
// normalize_skill_name (agent/src/claude_chat_model.py) -- same fields, same "agent:<name>"
// convention for a Task/Agent subagent launch, same fail-open contract (any read/parse problem
// here exits 0, never blocks -- an infrastructure gap must never masquerade as "skill required").
import { readFileSync } from "node:fs";

const required = (process.env.AIDW_REQUIRED_SKILLS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
if (required.length === 0) process.exit(0);

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as this repo's own
// .claude/hooks/graph-diagram-check.mjs --stop.
if (input.stop_hook_active) process.exit(0);

let transcript = "";
try {
  transcript = readFileSync(input.transcript_path, "utf8");
} catch {
  process.exit(0); // unreadable transcript -- fail open, same contract as read_skill_invocations
}

function normalizeSkillName(name) {
  return name.trim().replace(/^\/+/, "").split(":").pop().trim();
}

const invoked = new Set();
for (const line of transcript.split("\n")) {
  if (!line.trim()) continue;
  let entry;
  try {
    entry = JSON.parse(line);
  } catch {
    continue;
  }
  if (entry?.type !== "assistant") continue;
  const content = entry.message?.content;
  if (!Array.isArray(content)) continue;
  for (const block of content) {
    if (!block || block.type !== "tool_use") continue;
    if (block.name === "Skill" && typeof block.input?.skill === "string") {
      const n = normalizeSkillName(block.input.skill);
      if (n) invoked.add(n);
    } else if ((block.name === "Task" || block.name === "Agent") && typeof block.input?.subagent_type === "string") {
      const n = normalizeSkillName(block.input.subagent_type);
      if (n) invoked.add(`agent:${n}`);
    }
  }
}

const missing = required.filter((s) => !invoked.has(s));
if (missing.length === 0) process.exit(0);

process.stderr.write(
  `You must invoke these required skills before finishing this turn: ${missing.join(", ")}. ` +
    "Use the Skill tool now, follow what each one says, and redo any work under it before you stop.\n",
);
process.exit(2);
