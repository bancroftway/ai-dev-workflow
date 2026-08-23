"use client";

import { useEffect, useState } from "react";

/**
 * Reusable diff/patch viewer -- Part 2 Task 8. Genuinely separate from EventLogView.tsx (which is
 * this task's other new component) because Task 10's Gate UI needs the same rendering
 * independently (brief's own reasoning): a tool-call row's expanded detail and a gate's draft
 * diff have nothing else in common, but both need "here is a unified diff, colorized, truncated
 * if huge" and neither should reimplement it.
 *
 * Line coloring (added/removed/hunk/file-header) is hand-rolled on this app's OWN existing ad hoc
 * palette (emerald/red/blue/neutral -- the same vocabulary as MetricsBar.tsx's CHIP_CLASS/
 * AppShell.tsx's DOT_CLASS), not left to a third-party theme -- a library's own diff colors would
 * not match this app's palette and the brief is explicit: don't invent new colors/tokens.
 *
 * Per-token TEXT coloring within a line (real syntax highlighting, on top of the line-level
 * coloring above) is a progressive enhancement via `shiki`, loaded dynamically. shiki is not a new
 * dependency: it's already in node_modules, pulled in transitively by @copilotkit/react-core's own
 * `streamdown` (confirmed via `npm ls shiki`) -- the ladder's "already-installed dependency" rung,
 * per this Part's own Global Constraint that syntax highlighting is the one place a small focused
 * library may be justified. It is intentionally NOT load-bearing: `codeToTokens` runs in a
 * useEffect and only ever adds `color` to already-rendered plain text; if it's slow, fails, or the
 * package is ever removed from node_modules, the line below still renders correctly with plain
 * text and this component's real job -- diff structure and coloring -- is unaffected.
 */

const COLLAPSE_LINE_COUNT = 16;

type DiffLineKind = "add" | "remove" | "hunk" | "header" | "context";

const LINE_CLASS: Record<DiffLineKind, string> = {
  add: "bg-emerald-50 text-emerald-900",
  remove: "bg-red-50 text-red-900",
  hunk: "bg-blue-50 text-blue-800",
  header: "bg-neutral-100 text-neutral-500 font-medium",
  context: "text-neutral-700",
};

function classifyDiffLine(line: string): DiffLineKind {
  if (line.startsWith("+++") || line.startsWith("---")) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

/** Minimal unified-diff sniff test -- unlike a bare "starts with +/-" check (too easy to false-
 * positive on unrelated text), this requires an actual diff marker line somewhere in the text.
 * Exported so EventLogView.tsx (and Task 10's Gate UI later) can decide per-payload whether a
 * given string is worth routing through DiffView at all, rather than every caller re-deriving its
 * own heuristic. */
export function looksLikeDiff(text: string): boolean {
  if (!text) return false;
  return text.split("\n").some((line) => line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@ "));
}

/** Structural subset of shiki's ThemedToken -- kept local (not `import type` from "shiki") so a
 * missing/changed shiki package only ever affects the try/catch'd runtime import below, never a
 * static type-check dependency on an undeclared package. */
interface HighlightToken {
  content: string;
  color?: string;
}

export function DiffView({ diff, title }: { diff: string; title?: string }) {
  const [tokenLines, setTokenLines] = useState<HighlightToken[][] | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // No synchronous setTokenLines(null) reset here (react-hooks/set-state-in-effect) -- DiffView
    // instances are always mounted key'd per-event (EventLogView.tsx keys on event.seq), so `diff`
    // is stable for a given mounted instance in practice; `cancelled` below is what actually
    // matters, preventing a stale async result from a torn-down instance from ever landing.
    import("shiki")
      .then(({ codeToTokens }) => codeToTokens(diff, { lang: "diff", theme: "github-light" }))
      .then((result) => {
        if (!cancelled) setTokenLines(result.tokens);
      })
      .catch(() => {
        // Fail soft, on purpose (see module docstring) -- plain-text lines below already cover
        // this; a highlighter hiccup must never block the diff itself from rendering.
      });
    return () => {
      cancelled = true;
    };
  }, [diff]);

  const lines = diff.split("\n");
  const isLong = lines.length > COLLAPSE_LINE_COUNT;
  const visibleCount = expanded || !isLong ? lines.length : COLLAPSE_LINE_COUNT;

  return (
    <div className="overflow-hidden rounded-lg border border-neutral-200">
      {title && (
        <div className="border-b border-neutral-200 bg-neutral-50 px-3 py-1.5 text-xs font-medium text-neutral-600">
          {title}
        </div>
      )}
      <div className="overflow-x-auto font-mono text-xs leading-5">
        {lines.slice(0, visibleCount).map((line, i) => {
          const tokens = tokenLines?.[i];
          return (
            <div key={i} className={`whitespace-pre px-3 ${LINE_CLASS[classifyDiffLine(line)]}`}>
              {tokens
                ? tokens.map((token, j) => (
                    <span key={j} style={token.color ? { color: token.color } : undefined}>
                      {token.content}
                    </span>
                  ))
                : line || " "}
            </div>
          );
        })}
      </div>
      {isLong && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="w-full border-t border-neutral-200 bg-neutral-50 py-1 text-xs font-medium text-neutral-500 hover:bg-neutral-100"
        >
          {expanded ? "Show less" : `Show ${lines.length - COLLAPSE_LINE_COUNT} more lines`}
        </button>
      )}
    </div>
  );
}
