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
// fail-closed backstop for whenever this hook is unavailable, misconfigured, or bypassed some
// other way -- exactly the posture claude_chat_model.py's read_skill_invocations/
// read_full_file_reads already document for the same reason.
//
// Activated per-turn via the AIDW_REQUIRED_SKILLS env var (comma-separated skill/agent names),
// set only on the "draft" role's CLI invocation (claude_chat_model.py/copilot_chat_model.py's own
// _required_skills_env_prefix) -- every REQUIRED_SKILLS_BY_STAGE entry's full list is documented
// in that stage's OWN *_draft.md prompt (confirmed by reading all of them), so gating on draft
// alone needs no per-role subset logic and never falsely blocks an audit session whose own prompt
// never asked for these skills.
//
// BOTH PROVIDERS (user directive, 2026-09-17): confirmed live, via a real authenticated turn in
// this exact sandbox image, that GitHub Copilot CLI 1.0.86-2 has its own real Stop-hook mechanism
// (`copilot help config`'s `hooks`/`disableAllHooks` keys) -- its stdin JSON gives
// `stop_hook_active`/`transcript_path` under the SAME field names Claude Code uses, so nothing
// below needs to branch on which provider is running; only the transcript's own per-line shape
// differs, so this file recognizes BOTH:
//   - Claude: an assistant-role JSONL entry whose `message.content` contains a
//     `{"type":"tool_use","name":"Skill","input":{"skill":"<name>"}}` block (mirrored exactly from
//     claude_chat_model.read_skill_invocations/normalize_skill_name).
//   - Copilot: a dedicated `{"type":"skill.invoked","data":{"name":"<name>"}}` line -- no
//     plugin-qualified prefix to normalize (confirmed live: a real invocation recorded the bare
//     name directly, e.g. "customize-cloud-agent").
// Same fail-open contract either way: any read/parse problem here exits 0, never blocks -- an
// infrastructure gap must never masquerade as "skill required".
//
// UNVERIFIED (honestly flagged, not assumed): Copilot's subagent-launch event shape. The "agent:"
// prefix convention below (for a required entry like remediation's "agent:code-simplifier") is
// only empirically confirmed for Claude's Task/Agent tool_use block; Copilot's equivalent, if any,
// was not captured against a real transcript before writing this -- a stage requiring an
// "agent:"-prefixed skill under Copilot may not be enforced by this hook until that's confirmed
// live, same "confirm against reality, don't just assume" discipline this codebase applies
// everywhere else. Falls back to gates/skill_gate.py's post-hoc check regardless.
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
  // Copilot: a dedicated event, one per invocation -- no tool_use indirection to unwrap.
  if (entry?.type === "skill.invoked" && typeof entry.data?.name === "string") {
    const n = normalizeSkillName(entry.data.name);
    if (n) invoked.add(n);
    continue;
  }
  // Claude: a generic tool_use block inside an assistant-role message.
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
