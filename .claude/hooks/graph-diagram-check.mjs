#!/usr/bin/env node
// Keeps the workflow graph diagram in README.md honest.
//
// Hashes every file that defines the LangGraph pipeline and compares it against the hash stamped
// into README.md. A mismatch means the graph changed but the diagram did not.
//
//   node .claude/hooks/graph-diagram-check.mjs           PostToolUse: prints a note for Claude
//   node .claude/hooks/graph-diagram-check.mjs --stop    Stop: exit 2 blocks the turn
//   node .claude/hooks/graph-diagram-check.mjs --stamp   record the current sources as documented

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const readmePath = join(root, "README.md");
const stampPattern = /<!-- graph-source-sha256: [0-9a-f]{64}|<!-- graph-source-sha256: unstamped/;

// Files whose contents decide what the diagram says: node definitions, wiring, gates, and the
// prompts that name which skills each stage calls.
const files = [
  "agent/src/graph.py",
  "agent/src/app_discovery.py",
  "agent/src/tech_stack_signals.py",
  "agent/src/rebuild.py",
  "agent/src/preflight_nodes.py",
  "agent/src/session_index.py",
  "agent/src/finding_cluster_nodes.py",
  "agent/src/repo_scan.py",
  "agent/src/test_hardening_nodes.py",
  "agent/src/e2e_nodes.py",
  "agent/src/metrics_nodes.py",
  "agent/src/exit_nodes.py",
  "agent/src/quality_security/quality_nodes.py",
  "agent/src/quality_security/security_nodes.py",
  "agent/src/sessions_api.py",
];
const dirs = [
  ["agent/src/gates", ".py"],
  ["agent/src/prompts", ".md"],
];

const sources = [...files];
for (const [dir, ext] of dirs) {
  for (const name of readdirSync(join(root, dir)).sort()) {
    if (name.endsWith(ext)) sources.push(`${dir}/${name}`);
  }
}

const hash = createHash("sha256");
for (const rel of sources) {
  hash.update(rel);
  hash.update(readFileSync(join(root, rel)));
}
const current = hash.digest("hex");

const readme = readFileSync(readmePath, "utf8");
const stamp = `<!-- graph-source-sha256: ${current} -->`;

if (process.argv.includes("--stamp")) {
  const stamped = stampPattern.test(readme)
    ? readme.replace(/<!-- graph-source-sha256: [^>]* -->/, stamp)
    : `${readme.trimEnd()}\n\n${stamp}\n`;
  writeFileSync(readmePath, stamped);
  process.exit(0);
}

if (readme.includes(stamp)) process.exit(0);

const message =
  "The workflow graph diagram in README.md is stale: the sources that define the graph have " +
  "changed since it was last stamped. Update the mermaid diagram (and the stage box contents) " +
  "in README.md to match the current graph, then run: " +
  "node .claude/hooks/graph-diagram-check.mjs --stamp";

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
