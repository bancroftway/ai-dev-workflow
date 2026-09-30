#!/usr/bin/env node
// Stop hook: renders every _draft/manifest.json diagram entry's .mmd source through the SAME
// mermaid-cli invocation gates/diagram_gate.py's own real gate (verify_plan_diagrams -> _render_one)
// uses, before the plan draft/audit turn ends, instead of waiting a full draft->audit->verify
// round-trip to discover a Mermaid syntax error -- the single most shovel-ready gap an earlier
// research pass found in this whole area: @mermaid-js/mermaid-cli, its puppeteer config, and
// headless Chromium are already baked into this exact sandbox image (see the Dockerfile's own
// mermaid-cli/mermaid-puppeteer-config.json/PUPPETEER_EXECUTABLE_PATH sections), and the render
// needs only manifest.json's diagram entries + the .mmd files the model's own file edits already
// wrote this turn -- no thread_id/DB/run_id/live LangGraph state at all.
//
// SHELLS OUT to gates/diagram_render_checks.py (byte-identical staged copy at
// /opt/aidw-hooks/diagram_render_checks.py -- see that module's own docstring) -- the REAL
// render+classify implementation diagram_gate.py's own _render_one calls too, not a hand-ported
// reimplementation of the mmdc command/markers. Unlike diagram_gate.py's own _render_one (which
// goes through provider.exec_in_sandbox, an async remote-exec abstraction, because IT runs OUTSIDE
// the sandbox), this hook's Python call spawns mmdc directly via subprocess.run -- it's already
// running INSIDE the same container. SVG output goes to a scratch temp directory
// diagram_render_checks.py creates itself, never back into the repo checkout, so this hook can
// never trip write_scope_gate or leave a stray diff behind.
//
// INFRA VS SYNTAX (same discipline as diagram_gate.py's own docstring): an environment failure
// (missing puppeteer config, no browser binary, a crashed Chromium) is never fed back as "fix your
// Mermaid" -- this hook only BLOCKS (exit 2) on a genuine syntax failure; an infra failure is
// logged via reportFailOpen and never blocks the turn.
//
// TIMEOUT: booting headless Chromium via Puppeteer and rendering -- potentially several diagrams
// serially, since a plan can have more than one -- is the same expensive category as
// check-coverage-stop.mjs/check-quick-scan-stop.mjs, so this hook gets the same bumped ~300s
// Stop-hook timeout (registered in the Dockerfile), not the 30s default every other hook here uses.
//
// STAGE-SCOPED VIA AIDW_STAGE (final-review fix, 2026-09-30): the claim this comment used to make
// -- "a turn for any other stage simply never has this file" -- was wrong from the start:
// manifest.json is written early (preflight) and persists for the whole run, so this hook kept
// booting headless Chromium on every LATER stage's Stop event too. Now gated on AIDW_STAGE below,
// matching check-citation-drop-stop.mjs's already-correct pattern.
import { readFileSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-diagram-render-stop";
const stage = process.env.AIDW_STAGE || "unknown";

if (stage !== "plan" && stage !== "brownfield-plan") process.exit(0);

const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const DRAFT_DIAGRAMS_DIR = ".ai-dev-workflow/plan/_draft/diagrams";

// Kept comfortably under the Stop hook's own bumped ~300s timeout (registered in the Dockerfile),
// same margin check-coverage-stop.mjs's own REPLAY_TIMEOUT_MS/check-quick-scan-stop.mjs's own
// TOOL_TIMEOUT_MS leave for the rest of the hook's overhead.
const RENDER_TIMEOUT_MS = 240_000;

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

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

const diagramRefs = Array.isArray(manifestDoc?.diagrams) ? manifestDoc.diagrams : [];
if (diagramRefs.length === 0) process.exit(0); // no diagrams in this plan -- nothing to render

// Only diagrams whose .mmd file actually exists on disk -- a manifest entry with no sidecar file
// yet is check-plan-schema-stop.mjs's own manifest-orphan sweep's problem to report, not this
// hook's (avoids a duplicate/confusing message for the same root cause, same convention that
// hook's own wireframe-content-check loop already uses).
const diagrams = [];
for (const d of diagramRefs) {
  if (typeof d?.name !== "string") continue; // already reported by the schema hook
  const mmdPath = `${cwd}/${DRAFT_DIAGRAMS_DIR}/${d.name}.mmd`;
  if (!existsSync(mmdPath)) continue;
  diagrams.push({ name: d.name, mmd_path: mmdPath });
}
if (diagrams.length === 0) process.exit(0);

let failures;
try {
  const proc = spawnSync(
    "python3",
    ["/opt/aidw-hooks/diagram_render_checks.py", "--render-hook"],
    {
      input: JSON.stringify({ diagrams }),
      encoding: "utf8",
      timeout: RENDER_TIMEOUT_MS,
    },
  );
  if (proc.status === 0 && proc.stdout) {
    failures = JSON.parse(proc.stdout).failures || [];
  } else {
    reportFailOpen(HOOK_NAME, stage, "diagram_render_checks.py subprocess failed, timed out, or produced no output", cwd);
    process.exit(0); // infra gap -- never a false rejection
  }
} catch {
  reportFailOpen(HOOK_NAME, stage, "diagram_render_checks.py subprocess failed, timed out, or returned unparsable output", cwd);
  process.exit(0);
}

if (failures.length === 0) process.exit(0);

const infraFailures = failures.filter((f) => f.is_infra_failure);
const syntaxFailures = failures.filter((f) => !f.is_infra_failure);

// Infra failures are visible (telemetry) but never block -- the same "never tell the draft node to
// fix your Mermaid for an environment problem" posture as diagram_gate.py's own real gate.
for (const f of infraFailures) {
  reportFailOpen(HOOK_NAME, stage, `diagram '${f.name}' render failed (infra, not syntax): ${f.stderr_tail}`, cwd);
}

if (syntaxFailures.length === 0) process.exit(0);

process.stderr.write(
  "One or more diagrams failed to render with mermaid-cli -- fix the Mermaid syntax now, in this " +
    "same turn, before finishing:\n" +
    syntaxFailures.map((f) => `- ${f.name}: ${f.stderr_tail}`).join("\n") +
    "\n",
);
process.exit(2);
