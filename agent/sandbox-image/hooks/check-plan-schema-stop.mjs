#!/usr/bin/env node
// Stop hook: validates .ai-dev-workflow/plan/_draft/steps.json and manifest.json's SHAPE and
// referenced artifacts before the plan draft/audit turn ends, instead of waiting a full
// draft->audit->verify round-trip for gates/diagram_gate.py to report the same thing.
//
// Root-caused 2026-09-19 (income-investor session 5905ba13, run 73bc09ec): manifest.json's own
// diagram entries used the field name `type` where the schema requires `kind`
// (`{"name":"data-model","type":"er"}`) -- a one-word typo that survived the whole turn silently
// (writing JSON to a file has no schema enforcement the way `--json-schema`-constrained structured
// output does) and cost a full redraft lap to discover. The SAME run also escalated after 5 laps
// partly on plainly mechanical problems: a wireframe referenced in manifest.json whose sidecar
// .html file was never created, 7 wireframes exceeding the cap of 6, and a wireframe containing a
// forbidden <iframe>. Every one of these is checkable from files already in the working directory,
// with zero LLM judgment involved -- this hook catches all four in the SAME turn that caused them.
//
// SCHEMA SOURCE OF TRUTH: schemas/*.schema.json here are GENERATED, not hand-typed -- see
// agent/src/schemas.py's `export_hook_schemas`/`HOOK_SCHEMAS` (StepsFile/ManifestFile, composed
// from the exact PlanStep/PlanDiagramRef/PlanWireframeRef models gates/diagram_gate.py's own
// deterministic_verify already validates each entry against). Never hand-edit these two files --
// re-run `cd agent && python -m src.schemas --export-hook-schemas` after changing any of those
// models; that module's own self-check fails if they go stale. lib/json-schema-lite.mjs is the
// ONE generic walker every schema file here is validated through -- no hook re-encodes a model's
// field names as its own magic strings.
//
// WIREFRAME_FORBIDDEN/_SAFE_DIAGRAM_NAME_RE/MAX_WIREFRAMES/MAX_WIREFRAME_BYTES below are PORTED
// from gates/diagram_gate.py (check_wireframe, DIAGRAM_MAX_WIREFRAMES/DIAGRAM_MAX_WIREFRAME_BYTES
// config constants), not re-derived -- Node in the sandbox has no path to call Python, the same
// constraint that already keeps claude_chat_model.py's/copilot_chat_model.py's `_map_tool_names`
// as two independently-typed copies (see either file's own docstring). KEEP THESE IN SYNC WITH
// gates/diagram_gate.py BY HAND -- there is no automated drift guard for this half (unlike the
// schema files above, which regenerate mechanically); re-read that module's own regex list here
// whenever either changes.
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as check-citation-drop-stop.mjs:
// no AIDW_-prefixed env var gates this -- steps.json/manifest.json's own existence in the working
// directory (always /workspace/repo) is the entire scope check. A turn for any other stage simply
// never has these files, so this exits 0 immediately.
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { validate } from "./lib/json-schema-lite.mjs";

const STEPS_PATH = ".ai-dev-workflow/plan/_draft/steps.json";
const MANIFEST_PATH = ".ai-dev-workflow/plan/_draft/manifest.json";
const WIREFRAMES_DIR = ".ai-dev-workflow/plan/_draft/wireframes";
const DIAGRAMS_DIR = ".ai-dev-workflow/plan/_draft/diagrams";

/** Filenames in `dir` ending in `ext`, with `ext` stripped -- e.g. `login.html` -> `login`. Empty
 * array if `dir` doesn't exist (never this stage's turn, or the model hasn't created it yet).
 * Never throws. */
function listStems(dir, ext) {
  try {
    return readdirSync(dir)
      .filter((f) => f.endsWith(ext))
      .map((f) => f.slice(0, -ext.length));
  } catch {
    return [];
  }
}

// Ported from config.py's DIAGRAM_MAX_WIREFRAMES/DIAGRAM_MAX_WIREFRAME_BYTES (env-overridable
// there; a fixed value here is fine -- this hook is a same-turn NUDGE, not the authoritative gate,
// and using a stale default only means an operator-tuned cap takes one extra lap to be enforced
// here, never a false rejection since diagram_gate.py's own check still runs after).
const MAX_WIREFRAMES = 6;
const MAX_WIREFRAME_BYTES = 30 * 1024;
const SAFE_DIAGRAM_NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;

// Ported verbatim from gates/diagram_gate.py's `_WIREFRAME_FORBIDDEN` -- same order, same
// messages, so a report from this hook reads identically to one from the real gate.
const WIREFRAME_FORBIDDEN = [
  [/<\s*script\b/i, "contains a <script> tag"],
  [/<[a-zA-Z][^>]*\son\w+\s*=/i, "contains an inline on*= event handler"],
  [/(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:https?:)?\/\//i, "references an external URL"],
  [
    /(?:src|href|action|data|xlink:href)\s*=\s*["']?\s*(?:javascript|vbscript|data|file)\s*:/i,
    "uses a dangerous URL scheme (javascript:/vbscript:/data:/file:)",
  ],
  [/url\(\s*["']?\s*(?:https?:)?\/\//i, "references an external URL (css url())"],
  [/@import\b/i, "uses @import (external stylesheet)"],
  [/<\s*(?:iframe|object|embed|base|form)\b/i, "contains an embedding/navigation element (iframe/object/embed/base/form)"],
  [/<\s*meta\b[^>]*http-equiv/i, "contains <meta http-equiv> (refresh/CSP override)"],
];

/** Ported from gates/diagram_gate.py's `check_wireframe` -- returns a reason string, or null if
 * acceptable. */
function checkWireframe(screen, htmlSource) {
  if (!SAFE_DIAGRAM_NAME_RE.test(screen || "")) {
    return `screen name ${JSON.stringify(screen)} must match ${SAFE_DIAGRAM_NAME_RE.source} (letters, digits, _, - only)`;
  }
  if (Buffer.byteLength(htmlSource, "utf8") > MAX_WIREFRAME_BYTES) {
    return `wireframe ${JSON.stringify(screen)} exceeds ${Math.floor(MAX_WIREFRAME_BYTES / 1024)} KB -- simplify it`;
  }
  const lowered = htmlSource.toLowerCase();
  if (!lowered.includes("<html") && !lowered.includes("<body") && !lowered.includes("<div")) {
    return `wireframe ${JSON.stringify(screen)} does not look like an HTML page`;
  }
  for (const [pattern, reason] of WIREFRAME_FORBIDDEN) {
    if (pattern.test(htmlSource)) {
      return `wireframe ${JSON.stringify(screen)} ${reason} -- wireframes must be fully self-contained (inline CSS only)`;
    }
  }
  return null;
}

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

function readJson(relPath) {
  try {
    return JSON.parse(readFileSync(`${cwd}/${relPath}`, "utf8"));
  } catch {
    return undefined; // absent, unreadable, or invalid JSON -- reported separately per file below
  }
}

const problems = [];

const stepsDoc = readJson(STEPS_PATH);
const manifestDoc = readJson(MANIFEST_PATH);
if (stepsDoc === undefined && manifestDoc === undefined) process.exit(0); // not this stage's turn

let stepsSchema, manifestSchema;
try {
  stepsSchema = JSON.parse(readFileSync(new URL("./schemas/plan_steps.schema.json", import.meta.url)));
  manifestSchema = JSON.parse(readFileSync(new URL("./schemas/plan_manifest.schema.json", import.meta.url)));
} catch {
  process.exit(0); // schema files missing from the image -- an infra gap, never a false rejection
}

if (stepsDoc !== undefined) {
  for (const { path, message } of validate(stepsDoc, stepsSchema)) {
    problems.push(`${STEPS_PATH}${path.slice(1)}: ${message}`);
  }
}

if (manifestDoc !== undefined) {
  for (const { path, message } of validate(manifestDoc, manifestSchema)) {
    problems.push(`${MANIFEST_PATH}${path.slice(1)}: ${message}`);
  }
  // Schema-shape valid from here on -- safe to read .wireframes/.diagrams as the arrays they now
  // provably are. Skips this section entirely if the shape check above already failed on them, so
  // a malformed entry never also produces a confusing secondary error about a field that doesn't
  // exist yet.
  const wireframes = Array.isArray(manifestDoc.wireframes) ? manifestDoc.wireframes : [];
  const diagrams = Array.isArray(manifestDoc.diagrams) ? manifestDoc.diagrams : [];

  if (wireframes.length > MAX_WIREFRAMES) {
    problems.push(
      `${MANIFEST_PATH}: ${wireframes.length} wireframes exceeds the cap of ${MAX_WIREFRAMES} -- keep only the screens this plan actually changes.`,
    );
  }

  for (const wf of wireframes) {
    if (typeof wf?.screen !== "string") continue; // already reported by the schema check above
    const htmlPath = `${WIREFRAMES_DIR}/${wf.screen}.html`;
    if (!existsSync(`${cwd}/${htmlPath}`)) {
      problems.push(`manifest.json lists wireframe ${JSON.stringify(wf.screen)} but ${htmlPath} does not exist -- create it.`);
      continue;
    }
    let html;
    try {
      html = readFileSync(`${cwd}/${htmlPath}`, "utf8");
    } catch {
      continue; // race with the model's own in-flight write -- not this hook's problem to report
    }
    const reason = checkWireframe(wf.screen, html);
    if (reason) problems.push(reason);
  }

  for (const dg of diagrams) {
    if (typeof dg?.name !== "string") continue; // already reported by the schema check above
    const mmdPath = `${DIAGRAMS_DIR}/${dg.name}.mmd`;
    if (!existsSync(`${cwd}/${mmdPath}`)) {
      problems.push(`manifest.json lists diagram ${JSON.stringify(dg.name)} but ${mmdPath} does not exist -- create it.`);
    }
  }

  // The OTHER direction, ported from gates/diagram_gate.py's `_load_and_check_manifest`
  // (root-caused live 2026-09-19, income-investor session 598b633d, plan lap 1 --
  // this hook originally only checked "manifest references a file that's missing," not "a file
  // exists that manifest doesn't reference," and the SAME redraft the schema/existence checks
  // above were built to prevent still cost a lap on this exact inverse case). A wireframe/diagram
  // dropped from manifest.json without being named in retired_wireframe_screens/
  // retired_diagram_names leaves an orphaned sidecar file on disk -- same "explicit retirement
  // only, silence is not retirement" discipline the ledger itself enforces.
  const manifestScreens = new Set(wireframes.filter((wf) => typeof wf?.screen === "string").map((wf) => wf.screen));
  const retiredScreens = new Set(Array.isArray(manifestDoc.retired_wireframe_screens) ? manifestDoc.retired_wireframe_screens : []);
  for (const screen of listStems(`${cwd}/${WIREFRAMES_DIR}`, ".html")) {
    if (!manifestScreens.has(screen) && !retiredScreens.has(screen)) {
      problems.push(`${WIREFRAMES_DIR}/${screen}.html exists on disk but is not listed in manifest.json's wireframes (or retired_wireframe_screens).`);
    }
  }
  const manifestDiagramNames = new Set(diagrams.filter((d) => typeof d?.name === "string").map((d) => d.name));
  const retiredDiagramNames = new Set(Array.isArray(manifestDoc.retired_diagram_names) ? manifestDoc.retired_diagram_names : []);
  for (const name of listStems(`${cwd}/${DIAGRAMS_DIR}`, ".mmd")) {
    if (!manifestDiagramNames.has(name) && !retiredDiagramNames.has(name)) {
      problems.push(`${DIAGRAMS_DIR}/${name}.mmd exists on disk but is not listed in manifest.json's diagrams (or retired_diagram_names).`);
    }
  }
}

if (problems.length === 0) process.exit(0);

process.stderr.write(
  "The plan's own files have problems a deterministic check will reject at verify time -- fix " +
    "them now, in this same turn, before finishing:\n" +
    problems.map((p) => `- ${p}`).join("\n") +
    "\n",
);
process.exit(2);
