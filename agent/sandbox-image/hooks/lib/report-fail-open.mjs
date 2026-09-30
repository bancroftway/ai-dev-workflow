// Shared helper: every Stop hook in this image fails open on a missing tool, a timeout, or
// unparsable output -- exits 0, lets the turn continue, never blocks a session on a flaky check.
// That behavior stays exactly as-is (see each hook's own "-- fail open"/"infra gap" comments); what
// was missing is any record that it happened at all. `reportFailOpen` is the one call every one of
// those branches makes right before its existing `process.exit(0)`.
//
// Appends ONE JSON object per line to an append-only log rather than read-modify-writing a shared
// JSON file (manifest.json-style): multiple hooks can fail open in the SAME turn, and an append is
// the one write pattern with no read-then-write race between them. agent/src/exit_nodes.py reads
// this file (if present) and folds a plain-language summary into the human-facing exit report --
// see that module's own `_parse_hook_fail_opens`/`_render_hook_fail_opens_section`.
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

const LOG_PATH = ".ai-dev-workflow/hook-fail-opens.jsonl";

/** Records one fail-open. `cwd` is the sandbox repo root -- pass the hook's own `input.cwd` once
 * it's known; the default ("." -- the process's own cwd) is what every hook already falls back to
 * before stdin has been parsed (see e.g. check-plan-schema-stop.mjs's `const cwd = input.cwd || "."`),
 * so it's the correct fallback here too, not a new convention.
 *
 * Never throws -- a broken logging call must not become its own new way to fail closed. Silently
 * does nothing if the append itself fails (read-only filesystem, out of space, etc.); the hook's
 * own fail-open exit still happens unconditionally right after calling this. */
export function reportFailOpen(hookName, stage, reason, cwd = ".") {
  try {
    const path = `${cwd}/${LOG_PATH}`;
    mkdirSync(dirname(path), { recursive: true });
    const line = JSON.stringify({ ts: new Date().toISOString(), hook: hookName, stage, reason });
    appendFileSync(path, line + "\n");
  } catch {
    // Logging the fail-open is best-effort only -- see docstring above.
  }
}
