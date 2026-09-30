#!/usr/bin/env node
// Frontend hardcoding guard (plan §7): tabs, stage order and labels come from the backend pipeline
// descriptor, so a quoted stage-key literal in src/ is drift waiting to happen. Stage keys are read
// from the backend itself (agent/src/pipeline_layout.py), so a new stage is guarded automatically.
//
// Allowlist = a per-line marker, so a NEW literal in an already-allowlisted file is still caught:
//   const x = state.stages?.["plan"]; // stage-literal-ok: <why this bespoke UI needs it>
// Comment-only lines are ignored.
//
//   node scripts/check-no-stage-literals.mjs          # always runs
//   node scripts/check-no-stage-literals.mjs --hook   # Stop hook: skip unless src/ or the layout changed
import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

const root = join(import.meta.dirname, "..");
const run = (cmd, args, cwd = root) => execFileSync(cmd, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });

if (process.argv.includes("--hook")) {
  const changed = run("git", ["diff", "--name-only", "HEAD"]) + run("git", ["ls-files", "--others", "--exclude-standard", "src"]);
  if (!changed.split("\n").some((f) => f.startsWith("src/") || f === "agent/src/pipeline_layout.py")) process.exit(0);
}

const keys = run("uv", ["run", "python", "-c", "from src.pipeline_layout import PIPELINE as P;print('|'.join(P.order))"], join(root, "agent")).trim();
if (!keys) throw new Error("no stage keys from agent/src/pipeline_layout.py");
const literal = new RegExp(`["'\`](${keys.replace(/[.*+?^${}()[\]\\]/g, "\\$&")})["'\`]`);

const hits = [];
function walk(dir) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(ts|tsx|js|jsx|mjs)$/.test(e.name)) {
      readFileSync(p, "utf8").split("\n").forEach((line, i) => {
        const t = line.trim();
        if (/^(\/\/|\*|\/\*|\{\/\*)/.test(t) || line.includes("stage-literal-ok")) return;
        if (literal.test(line.replace(/\s\/\/.*$/, ""))) hits.push(`${relative(root, p).replaceAll("\\", "/")}:${i + 1}: ${t}`);
      });
    }
  }
}
walk(join(root, "src"));

if (hits.length) {
  console.error(
    `Quoted stage-key literal(s) in src/ -- read stages from the pipeline descriptor (src/lib/pipeline.tsx) instead,\n` +
      `or, for genuinely bespoke UI, add "// stage-literal-ok: <reason>" on that line:\n  ${hits.join("\n  ")}`,
  );
  process.exit(process.argv.includes("--hook") ? 2 : 1);
}
console.log("check-no-stage-literals: OK");
