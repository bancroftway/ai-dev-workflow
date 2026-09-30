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
// .html file was never created, and a wireframe containing a forbidden <iframe> (a THIRD problem
// that run hit, a wireframe-count cap, was removed 2026-09-24 -- there is deliberately no upper
// limit on wireframe count now). Every one of these is checkable from files already in the working
// directory, with zero LLM judgment involved -- this hook catches them in the SAME turn that
// caused them.
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
// WIREFRAME-CONTENT CHECK (check_wireframe) and the MANIFEST-ORPHAN SWEEP below used to be hand-
// ported from gates/diagram_gate.py (MAX_WIREFRAME_BYTES/SAFE_DIAGRAM_NAME_RE/WIREFRAME_FORBIDDEN/
// check_wireframe, and _load_and_check_manifest's own disk-vs-manifest set-difference logic) --
// "KEEP THESE IN SYNC WITH gates/diagram_gate.py BY HAND, no automated drift guard". Both now live
// in gates/wireframe_linkage_checks.py (byte-identical staged copy at
// /opt/aidw-hooks/wireframe_linkage_checks.py, same as the linkage checks
// check-plan-citations-stop.mjs already shells out to), so this hook SHELLS OUT to the REAL
// implementation instead of a second, independently-drifting copy (2026-09-29, Task 9). This hook
// still does its own directory listing (readdirSync) and file reads -- the shared module has no
// sandbox access by design; it only ever sees data this hook already read and handed over. There
// is deliberately no wireframe COUNT cap (removed 2026-09-24) -- a plan may cite as many wireframes
// as the work actually needs; do not reintroduce one.
//
// PROVIDER- AND STAGE-AGNOSTIC BY CONSTRUCTION, same reasoning as check-citation-drop-stop.mjs:
// no AIDW_-prefixed env var gates this -- steps.json/manifest.json's own existence in the working
// directory (always /workspace/repo) is the entire scope check. A turn for any other stage simply
// never has these files, so this exits 0 immediately.
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { validate } from "./lib/json-schema-lite.mjs";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-plan-schema-stop";
const stage = process.env.AIDW_STAGE || "unknown";

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

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

// One nudge per turn, never a loop -- same convention as every other Stop hook in this image.
if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

function readJson(relPath) {
  const path = `${cwd}/${relPath}`;
  if (!existsSync(path)) return undefined; // not this stage's turn, or file not written yet
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    // Present but unreadable/invalid JSON -- a genuine fail-open, unlike the routine absence above.
    reportFailOpen(HOOK_NAME, stage, `unreadable or invalid JSON: ${relPath}`, cwd);
    return undefined;
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
  reportFailOpen(HOOK_NAME, stage, "schema files missing from the image (schemas/plan_steps.schema.json / plan_manifest.schema.json)", cwd);
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

  // Read each wireframe's real HTML body -- needed for the content check below (shelled out to
  // Python, which has no sandbox access of its own). A wireframe manifest.json references but that
  // doesn't exist on disk isn't reported HERE any more -- that's the orphan sweep's manifest->disk
  // direction below, so this loop only SKIPS one it can't read rather than reporting anything of
  // its own (avoids a duplicate message from two different checks for the same root cause).
  const wireframeHtml = [];
  for (const wf of wireframes) {
    if (typeof wf?.screen !== "string") continue; // already reported by the schema check above
    const htmlPath = `${cwd}/${WIREFRAMES_DIR}/${wf.screen}.html`;
    if (!existsSync(htmlPath)) continue;
    try {
      wireframeHtml.push({ screen: wf.screen, html_source: readFileSync(htmlPath, "utf8") });
    } catch {
      // race with the model's own in-flight write -- not this hook's problem to report
    }
  }

  // WIREFRAME-CONTENT CHECK (check_wireframe) + MANIFEST-ORPHAN SWEEP (both directions, both
  // artifact kinds -- root-caused live 2026-09-19, income-investor session 598b633d, plan lap 1: a
  // wireframe/diagram dropped from manifest.json without being named in
  // retired_wireframe_screens/retired_diagram_names leaves an orphaned sidecar file on disk, same
  // "explicit retirement only, silence is not retirement" discipline the ledger itself enforces).
  // SHELLS OUT to the REAL implementation, gates/wireframe_linkage_checks.py (see this file's own
  // header), instead of a hand-ported JS copy.
  try {
    const proc = spawnSync(
      "python3",
      ["/opt/aidw-hooks/wireframe_linkage_checks.py", "--check-hook"],
      {
        input: JSON.stringify({
          wireframe_html: wireframeHtml,
          disk_wireframe_screens: listStems(`${cwd}/${WIREFRAMES_DIR}`, ".html"),
          manifest_wireframe_screens: wireframes.filter((wf) => typeof wf?.screen === "string").map((wf) => wf.screen),
          retired_wireframe_screens: Array.isArray(manifestDoc.retired_wireframe_screens) ? manifestDoc.retired_wireframe_screens : [],
          disk_diagram_names: listStems(`${cwd}/${DIAGRAMS_DIR}`, ".mmd"),
          manifest_diagram_names: diagrams.filter((d) => typeof d?.name === "string").map((d) => d.name),
          retired_diagram_names: Array.isArray(manifestDoc.retired_diagram_names) ? manifestDoc.retired_diagram_names : [],
        }),
        encoding: "utf8",
        timeout: 20000,
      },
    );
    if (proc.status === 0 && proc.stdout) {
      const result = JSON.parse(proc.stdout);
      problems.push(...(result.wireframe_content_problems || []));
      problems.push(...(result.manifest_orphan_problems || []));
    } else {
      reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or produced no output", cwd);
    }
  } catch {
    reportFailOpen(HOOK_NAME, stage, "wireframe_linkage_checks.py subprocess failed, timed out, or returned unparsable output", cwd);
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
