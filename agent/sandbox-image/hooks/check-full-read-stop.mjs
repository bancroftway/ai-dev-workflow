#!/usr/bin/env node
// Stop hook: proves an AUDIT session's OWN transcript covers the ENTIRE target file with Read
// calls before the turn ends, instead of waiting a full draft->audit->verify round-trip for
// graph.py's `_verify_specification_ledger` / gates/diagram_gate.py's `_load_and_sync_plan_steps`
// (the "Review-depth safety net") to report the identical thing.
//
// Root-caused 2026-09-18 (income-investor session 6244ef47): the deterministic gate this hook
// front-runs failed 5 straight specification verify laps on an audit whose transcript showed one
// full Read of the file EVERY lap -- the gate's own session-key lookup was the bug there (fixed
// separately in graph.py/gates/diagram_gate.py), not the audit's behavior. THIS hook exists so a
// genuinely partial re-read gets caught and corrected in the SAME turn, the moment it happens,
// rather than costing a whole redraft lap to discover after the fact -- purely additive to that
// fix, not a replacement for it (the deterministic gate is the fail-closed backstop for whenever
// this hook is unavailable, misconfigured, or bypassed some other way, same posture as every
// other hook in this image).
//
// ACTIVATED VIA AIDW_AUDIT_FULL_READ_FILE (claude_chat_model.py's/copilot_chat_model.py's
// `_full_read_env_prefix`, config.py's `AUDIT_FULL_READ_FILE_BY_STAGE`), set ONLY on the AUDIT
// role's CLI invocation -- mirrors require-skills-stop.mjs's own AIDW_REQUIRED_SKILLS activation
// exactly (draft-only there, audit-only here -- see `_full_read_env_prefix`'s own docstring for
// why the two are inverted).
//
// PROVIDER ASYMMETRY, DISCLOSED, NOT A BUG: this env var is NEVER set for a Copilot turn --
// copilot_chat_model.py's own `_full_read_env_prefix` hard-returns "" unconditionally, because
// this codebase has never confirmed what a generic Read tool call looks like in a real Copilot
// transcript (only the dedicated `skill.invoked` event require-skills-stop.mjs relies on is
// confirmed live). Guessing at that shape here would risk a FALSE "file not read" rejection on a
// Copilot turn that read the file correctly in a format this parser doesn't recognize -- worse
// than no hook at all. So this hook, unlike the other three in this image, only ever ACTIVATES
// for Claude; for Copilot the env var check below always short-circuits it to exit 0. Extend the
// Claude-only parsing block below once a live Copilot transcript sample confirms its own shape,
// the same "confirm against reality" bar every other transcript-parsing function in this codebase
// already holds itself to.
//
// TRANSCRIPT ALGORITHM: this turn's own transcript file is already on disk inside the sandbox
// (`input.transcript_path`), so parsing it into Read-call line ranges (`computeReadRanges` below)
// needs no sandbox-exec round trip and stays exactly as it was -- same offset/limit defaults, same
// suffix-based path matching as claude_chat_model.py's `read_full_file_reads` (that function's own
// docstring: "confirmed against this very module's own captured transcript... a real session
// showing plain reads, offset+limit reads, and offset-only reads"). READ_TOOL_DEFAULT_WINDOW_LINES
// below is ported from config.py's own constant of the same name (2000) -- KEEP IN SYNC BY HAND if
// that ever changes; no automated drift guard for this one (it is a plain int literal, not
// something export_hook_schemas can generate).
//
// WHY A PYTHON SUBPROCESS FOR THE COVERAGE CHECK, NOT A JS PORT (2026-09-29): only the pure
// whole-file interval-union check itself (given the ranges above, do they cover every line with no
// gap?) used to be hand-ported here as its own JS function, duplicating claude_chat_model.py's
// private interval-union helper with no automated drift guard. That check now lives in
// full_read_checks.py (extracted from claude_chat_model.py, which imports it back unchanged),
// COPY'd in below UNMODIFIED and stdlib-only, so this hook shells out to the REAL implementation
// for that one piece instead of a second, independently-drifting copy. The transcript-parsing
// above stays exactly where it was -- it needs no sandbox access claude_chat_model.py's own
// version doesn't already have from a different source, so there is nothing to dedupe there.
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { reportFailOpen } from "./lib/report-fail-open.mjs";

const HOOK_NAME = "check-full-read-stop";
const stage = process.env.AIDW_STAGE || "unknown";

const READ_TOOL_DEFAULT_WINDOW_LINES = 2000;

/** Scans a Claude transcript's assistant-role lines for `Read` tool_use blocks targeting
 * `targetPath` (matched by suffix, since the transcript's own `input.file_path` is typically
 * absolute inside the sandbox while `targetPath` here is the relative path this pipeline always
 * passes). Returns the collected (start, end) ranges -- empty if none found. */
function computeReadRanges(transcriptText, targetPath) {
  const target = targetPath.replace(/\\/g, "/").replace(/^\/+/, "");
  const ranges = [];
  for (const line of transcriptText.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let entry;
    try {
      entry = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (entry?.type !== "assistant") continue;
    const content = entry.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (!block || block.type !== "tool_use" || block.name !== "Read") continue;
      const readPath = block.input?.file_path;
      if (typeof readPath !== "string") continue;
      const normalized = readPath.replace(/\\/g, "/").replace(/^\/+/, "");
      if (normalized !== target && !normalized.endsWith(`/${target}`)) continue;
      const offset = block.input?.offset;
      const limit = block.input?.limit;
      const start = typeof offset === "number" && offset > 0 ? offset : 1;
      const span = typeof limit === "number" && limit > 0 ? limit : READ_TOOL_DEFAULT_WINDOW_LINES;
      ranges.push([start, start + span - 1]);
    }
  }
  return ranges;
}

const targetPath = process.env.AIDW_AUDIT_FULL_READ_FILE;
if (!targetPath) process.exit(0); // not an audit turn for a stage this hook covers -- see header

let input = {};
try {
  input = JSON.parse(readFileSync(0, "utf8"));
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable or invalid stdin JSON");
  process.exit(0); // no readable stdin -- fail open
}

if (input.stop_hook_active) process.exit(0);

const cwd = input.cwd || ".";

let fileContent;
try {
  fileContent = readFileSync(`${cwd}/${targetPath}`, "utf8");
} catch {
  process.exit(0); // file absent -- nothing to have proven a full read of yet
}
const totalLines = fileContent.split("\n").length;

let transcriptText;
try {
  transcriptText = readFileSync(input.transcript_path, "utf8");
} catch {
  reportFailOpen(HOOK_NAME, stage, "unreadable transcript file", cwd);
  process.exit(0); // unreadable transcript -- fail open, same contract as the Python backstop
}

const ranges = computeReadRanges(transcriptText, targetPath);

let covered = false;
if (ranges.length > 0) {
  try {
    const proc = spawnSync("python3", ["/opt/aidw-hooks/full_read_checks.py", "--check-hook"], {
      input: JSON.stringify({ ranges, total_lines: totalLines }),
      encoding: "utf8",
      timeout: 20000,
    });
    if (proc.status !== 0 || !proc.stdout) {
      reportFailOpen(HOOK_NAME, stage, "full_read_checks.py subprocess failed, timed out, or produced no output", cwd);
      process.exit(0); // infra gap -- never a false rejection
    }
    covered = Boolean(JSON.parse(proc.stdout).covered);
  } catch {
    reportFailOpen(HOOK_NAME, stage, "unparsable full_read_checks.py output", cwd);
    process.exit(0);
  }
}

if (covered) process.exit(0);

process.stderr.write(
  `You must view the WHOLE ${targetPath} (${totalLines} lines) with your Read tool this pass, not ` +
    "a partial read, before finishing this turn -- old content is not exempt from scrutiny just " +
    "because a prior lap already touched it. Use one unparameterized Read (or offset/limit reads " +
    "that together cover every line) now.\n",
);
process.exit(2);
