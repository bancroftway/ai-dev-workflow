#!/usr/bin/env node
// Keeps two hand-written README.md sections honest: the workflow graph diagram and the
// deterministic gate/hook inventory table.
//
// Each section has its own source list. The script hashes those sources and compares against the
// hash stamped into README.md under that section's key. A mismatch means the code changed but the
// section did not.
//
//   node .claude/hooks/graph-diagram-check.mjs                  PostToolUse: prints a note for Claude
//   node .claude/hooks/graph-diagram-check.mjs --stop           Stop: exit 2 blocks the turn
//   node .claude/hooks/graph-diagram-check.mjs --stamp [key]    record the current sources as documented
//                                                                (one key, or every key when omitted)

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const readmePath = join(root, "README.md");

const nodeClusters = [
  "agent/src/graph.py",
  "agent/src/e2e_nodes.py",
  "agent/src/metrics_nodes.py",
  "agent/src/exit_nodes.py",
  "agent/src/test_hardening_nodes.py",
];

// Files whose contents decide what each section says.
const sections = [
  {
    key: "graph-source",
    what: "workflow graph diagram",
    fix: "Update the mermaid diagram (and the stage box contents) in README.md to match the current graph",
    files: [
      ...nodeClusters,
      "agent/src/app_discovery.py",
      "agent/src/tech_stack_signals.py",
      "agent/src/rebuild.py",
      "agent/src/preflight_nodes.py",
      "agent/src/session_store.py",
      "agent/src/repo_scan.py",
      "agent/src/sessions_api.py",
    ],
    dirs: [
      ["agent/src/gates", [".py"]],
      ["agent/src/prompts", [".md"]],
    ],
  },
  {
    key: "gate-inventory",
    what: "deterministic gates and hooks table",
    fix: "Update the '## Deterministic gates and hooks' table in README.md to match the current gate/hook code (add, remove or adjust rows)",
    files: [
      ...nodeClusters,
      "agent/src/config.py",
      "agent/src/schemas.py",
      "agent/src/spec_ledger.py",
      "agent/src/chat_model.py",
      "agent/src/claude_chat_model.py",
      "agent/src/repo_scan.py",
      ".claude/settings.json",
    ],
    dirs: [
      ["agent/src/gates", [".py"]],
      ["agent/sandbox-image/hooks", [".mjs", ".py"]],
      ["agent/sandbox-image/hooks/lib", [".mjs"]],
      [".claude/hooks", [".mjs"]],
    ],
  },
];

function hashSection(section) {
  const sources = [...section.files];
  for (const [dir, exts] of section.dirs) {
    for (const name of readdirSync(join(root, dir)).sort()) {
      if (exts.some((ext) => name.endsWith(ext))) sources.push(`${dir}/${name}`);
    }
  }
  const hash = createHash("sha256");
  for (const rel of sources) {
    hash.update(rel);
    hash.update(readFileSync(join(root, rel)));
  }
  return hash.digest("hex");
}

const stampFor = (key, hash) => `<!-- ${key}-sha256: ${hash} -->`;
const stampPattern = (key) => new RegExp(`<!-- ${key}-sha256: [^>]* -->`);

let readme = readFileSync(readmePath, "utf8");

if (process.argv.includes("--stamp")) {
  const only = process.argv[process.argv.indexOf("--stamp") + 1];
  if (only && !sections.some((s) => s.key === only)) {
    process.stderr.write(`Unknown stamp key "${only}". Known: ${sections.map((s) => s.key).join(", ")}\n`);
    process.exit(1);
  }
  for (const section of sections) {
    if (only && section.key !== only) continue;
    const stamp = stampFor(section.key, hashSection(section));
    readme = stampPattern(section.key).test(readme)
      ? readme.replace(stampPattern(section.key), stamp)
      : `${readme.trimEnd()}\n${stamp}\n`;
  }
  writeFileSync(readmePath, readme);
  process.exit(0);
}

const stale = sections.filter((s) => !readme.includes(stampFor(s.key, hashSection(s))));
if (stale.length === 0) process.exit(0);

const message = stale
  .map(
    (s) =>
      `The ${s.what} in README.md is stale: the sources that define it have changed since it was ` +
      `last stamped. ${s.fix}, then run: node .claude/hooks/graph-diagram-check.mjs --stamp ${s.key}`
  )
  .join("\n");

if (process.argv.includes("--stop")) {
  // Never block twice in a row on the same turn -- Claude gets one nudge, not a loop.
  let stopHookActive = false;
  try {
    stopHookActive = JSON.parse(readFileSync(0, "utf8")).stop_hook_active === true;
  } catch {
    // No stdin payload (manual run) -- treat as a first pass.
  }
  if (stopHookActive) process.exit(0);
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

process.stdout.write(
  JSON.stringify({
    hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext: message },
  })
);
