// Shared helper: extracts the FINAL assistant text response from a Claude transcript and parses it
// as JSON -- the shape check-adversarial-stop.mjs/check-remediation-stop.mjs both need, since
// neither stage writes its own structured report to a repo file the way e.g. plan's steps.json
// does. Both are dispatched via structured_output.ainvoke_structured (graph.py's make_draft_node),
// whose whole contract is "respond with a single JSON object matching the schema as your FINAL
// message" -- not a client-side tool call, not a file write. Same transcript-parsing precedent as
// check-full-read-stop.mjs/require-skills-stop.mjs (this turn's own transcript is already on disk
// at `input.transcript_path`, no sandbox-exec round trip needed) -- this generalizes their "scan
// assistant-role JSONL lines" step for a plain TEXT response instead of a tool_use block.
//
// Deliberately last-text-wins, not "must be the very last transcript line": a turn can carry
// commentary text alongside earlier tool_use blocks before the real final answer, so this collects
// every assistant message's own text content and keeps only the most recent one.
//
// Code-fence stripping mirrors structured_output.py's own `_CODE_FENCE_RE` (kept in sync by hand --
// a two-line regex, not the parser-scale duplication risk coverage_parsing.py's own docstring warns
// against): the model is asked for bare JSON but a fenced response is common enough that this
// same-turn hook should tolerate it exactly like the host-side parser does.
//
// Pure (operates on already-read transcript text, does no I/O of its own) so each caller reads its
// own `input.transcript_path` and reports an unreadable file as the genuine problem it is --
// exactly check-full-read-stop.mjs's own convention (`reportFailOpen(..., "unreadable transcript
// file", ...)`), which a read failure buried inside this helper could not distinguish from the
// routine "no final JSON yet" case below.
const CODE_FENCE_RE = /^```(?:json)?\s*|\s*```$/gm;

/** The parsed JSON object from the transcript's own last assistant text response, or null if it
 * has no assistant text, or that text doesn't parse as JSON. Never throws. */
export function extractFinalJson(transcriptText) {
  let lastText = null;
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
    const text = content
      .filter((block) => block?.type === "text" && typeof block.text === "string")
      .map((block) => block.text)
      .join("\n")
      .trim();
    if (text) lastText = text;
  }
  if (!lastText) return null;

  const stripped = lastText.replace(CODE_FENCE_RE, "").trim();
  try {
    return JSON.parse(stripped);
  } catch {
    return null;
  }
}
