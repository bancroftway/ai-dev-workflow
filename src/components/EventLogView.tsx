"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { Chip } from "@/components/MetricsBar";
import { DiffView, looksLikeDiff } from "@/components/DiffView";
import { ViewContainer } from "@/components/ViewContainer";
import { formatDuration, parseEventTs, toolNameOf, useRunEvents, type RunLogEvent } from "@/lib/use-run-events";

/**
 * Folding tool-call-row event log -- Part 2 Task 8. Standalone/reusable on purpose (brief's own
 * instruction): Task 13 decides which tab this lives in, this component only needs a session_id
 * to render itself anywhere it's mounted.
 *
 * Data comes from useRunEvents() (src/lib/use-run-events.ts) -- factored out from this component's
 * own original fetch/merge effects when Task 9 needed the identical data for a second component
 * (Swimlane.tsx). See that hook's own docstring for the full "why" (merges Task 1/2's two real
 * destinations for the same underlying event, deduped by `seq`, no polling); this file no longer
 * owns that plumbing, only how the resulting event list renders.
 *
 * Part 2 Task 12 added the scroll-anchoring/live-follow behavior below (`useStickToBottom`): the
 * row list is now its own bounded, independently-scrolling region (not the page-level scroll
 * AppShell's `<main>` owns) specifically so it can be pinned to the newest row while a run is
 * live, exactly like a chat/terminal log.
 */

/** "Close enough to the bottom to count as at the bottom," in px -- a couple of row-heights of
 * slack so sub-pixel scroll math never misses an exact-0 comparison. */
const BOTTOM_THRESHOLD_PX = 24;

/** Pin-to-bottom / live-follow for the scrollable event list -- this Part's own explicitly-
 * flagged footgun ("a naive implementation fights the user's own scroll position"). Auto-follow
 * starts ON (a freshly opened log shows the newest event first, same convention as a chat/
 * terminal); any manual scroll away from the bottom -- wheel, touch, keyboard, drag all funnel
 * through the same native `scroll` event on this one container, so a single listener covers every
 * input method -- turns it off. Scrolling back within BOTTOM_THRESHOLD_PX turns it back on. While
 * off, a new arrival never moves the scrollbar; `newCount`/`jumpToLatest` back the "N new events"
 * pill instead. */
function useStickToBottom(events: RunLogEvent[]) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [stuckToBottom, setStuckToBottom] = useState(true);
  // The newest seq that existed at the moment the user scrolled away -- null while stuck to the
  // bottom (nothing "missed" yet) or once re-engaged. State, not a ref: eslint's react-hooks/refs
  // rule (correctly) forbids reading ref.current during render, and `newCount` below needs this
  // value during render to decide whether to show the pill.
  const [lastSeenSeq, setLastSeenSeq] = useState<number | null>(null);

  // useLayoutEffect (not useEffect): this adjusts scroll position after the new rows are in the
  // DOM but before the browser paints, so a pinned view never shows one visible frame at the old
  // position before jumping -- React's own documented case for this hook.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el || !stuckToBottom) return;
    el.scrollTop = el.scrollHeight;
  }, [events, stuckToBottom]);

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_THRESHOLD_PX;
    setStuckToBottom(atBottom);
    // Snapshot once, right as the user leaves the bottom (prev ?? ...) -- must NOT keep advancing
    // on every later scroll tick while they stay away, or newCount would never grow. Cleared back
    // to null on re-arrival so the next disengage starts a fresh snapshot.
    setLastSeenSeq((prev) => (atBottom ? null : prev ?? events[events.length - 1]?.seq ?? null));
  }

  const newCount = stuckToBottom || lastSeenSeq == null ? 0 : events.filter((e) => e.seq > lastSeenSeq).length;

  function jumpToLatest() {
    setStuckToBottom(true);
    setLastSeenSeq(null);
  }

  return { containerRef, onScroll, newCount, jumpToLatest };
}

/** Same dot-color vocabulary as AppShell.tsx's own DOT_CLASS (blue/amber/emerald/red) plus the
 * neutral tones MetricsBar.tsx's CHIP_CLASS already uses for "no strong signal" -- not a new
 * palette, DOT_CLASS itself just isn't exported for reuse here. */
const TYPE_DOT: Record<RunLogEvent["type"], string> = {
  node_started: "bg-blue-500",
  node_finished: "bg-emerald-500",
  tool_call: "bg-neutral-400",
  reasoning: "bg-neutral-300",
  gate_paused: "bg-amber-500",
  gate_resolved: "bg-emerald-500",
};

function truncateOneLine(s: string, max: number): string {
  const oneLine = s.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? `${oneLine.slice(0, max)}…` : oneLine;
}

/** One-line arg preview for the dense row. Claude's shape wraps args in `payload.input`; Copilot's
 * uncorrelated shape (no confirmed real example yet -- copilot_chat_model.py's own docstring) has
 * no such wrapper, so this also tries a couple of plausible top-level keys directly on `payload`
 * before giving up and showing no arg summary at all -- never throws, never assumes either shape. */
function argSummary(payload: Record<string, unknown> | null): string | null {
  if (!payload) return null;
  const input = payload.input;
  if (input && typeof input === "object") {
    const obj = input as Record<string, unknown>;
    const preferred = obj.command ?? obj.file_path ?? obj.path ?? obj.pattern;
    if (typeof preferred === "string") return truncateOneLine(preferred, 80);
  }
  if (typeof input === "string") return truncateOneLine(input, 80);
  const direct = payload.path ?? payload.command ?? payload.file;
  return typeof direct === "string" ? truncateOneLine(direct, 80) : null;
}

/** NODE_FINISHED's duration, when a matching NODE_STARTED for the same (run_id, stage, node)
 * precedes it. graph.py's draft/audit/verify nodes emit NODE_STARTED as of Part 2 Task 9 (see
 * task-9-report.md) -- before that, no call site emitted it and this never fired against real
 * data; the type had carried NODE_STARTED since Task 1 specifically so a later call site could add
 * it without a schema change, and Task 9 is that call site. An unmatched NODE_STARTED (a node that
 * started but errored before reaching its own NODE_FINISHED) is left in `openStarts` and simply
 * never produces a duration -- not a bug, see Swimlane.tsx for the view that renders that case
 * explicitly as an open-ended span rather than silently dropping it. */
function computeDurations(events: RunLogEvent[]): Map<number, number> {
  const durations = new Map<number, number>();
  const openStarts = new Map<string, RunLogEvent>();
  for (const e of events) {
    const key = `${e.run_id}|${e.stage ?? ""}|${e.node ?? ""}`;
    if (e.type === "node_started") {
      openStarts.set(key, e);
    } else if (e.type === "node_finished") {
      const start = openStarts.get(key);
      if (start) {
        const ms = parseEventTs(e.ts) - parseEventTs(start.ts);
        if (ms >= 0) durations.set(e.seq, ms);
        openStarts.delete(key);
      }
    }
  }
  return durations;
}

/** A consecutive run of same-tool TOOL_CALL events -- any other event (including a lone/unpaired
 * tool call) is just a run of length 1, rendered without group chrome. */
interface Run {
  tool: string | null;
  events: RunLogEvent[];
}

function groupConsecutive(events: RunLogEvent[]): Run[] {
  const runs: Run[] = [];
  for (const e of events) {
    const tool = toolNameOf(e);
    const last = runs[runs.length - 1];
    if (tool && last && last.tool === tool) {
      last.events.push(e);
    } else {
      runs.push({ tool, events: [e] });
    }
  }
  return runs;
}

/** Finding 3 (Phase E audit): "prose stays open" -- the Spec's own stated core lesson, previously
 * unmet because REASONING had no producer at all. E-3a gave it a real one (both providers,
 * redacted); this is that event type's dedicated render path -- every other type still goes
 * through EventRow's dense one-liner below. groupConsecutive (above) never merges a `reasoning`
 * event into a tool run (`toolNameOf` returns null for it), so every one always arrives here as its
 * own length-1 run; the `run.events.length === 1` guard at the call site is defense in depth, not
 * load-bearing. */
function isLoneReasoning(run: Run): run is Run & { events: [RunLogEvent] } {
  return run.events.length === 1 && run.events[0].type === "reasoning";
}

export function EventLogView() {
  const events = useRunEvents();
  const durations = computeDurations(events);
  const runs = groupConsecutive(events);
  const { containerRef, onScroll, newCount, jumpToLatest } = useStickToBottom(events);

  return (
    <ViewContainer>
      <div className="shrink-0">
        <h1 className="text-lg font-semibold">Event Log</h1>
        <p className="text-sm text-neutral-500">Every node, tool call, and gate captured for this run, oldest first.</p>
      </div>
      {events.length === 0 ? (
        <p className="text-sm text-neutral-400">No events yet.</p>
      ) : (
        <div className="relative min-h-0 flex-1">
          <div
            ref={containerRef}
            onScroll={onScroll}
            tabIndex={0}
            aria-label="Event log"
            className="flex h-full flex-col divide-y divide-neutral-100 overflow-y-auto rounded-lg border border-neutral-200"
          >
            {runs.map((run) => {
              if (isLoneReasoning(run)) {
                return <ReasoningRow key={run.events[0].seq} event={run.events[0]} />;
              }
              return run.tool && run.events.length > 1 ? (
                <GroupRow key={run.events[0].seq} tool={run.tool} events={run.events} durations={durations} />
              ) : (
                <EventRow key={run.events[0].seq} event={run.events[0]} duration={durations.get(run.events[0].seq)} />
              );
            })}
          </div>
          {/* Shown only while auto-follow is disengaged AND real content arrived since -- blue,
              not amber/red: this is a neutral "there's more" nudge, not a warning, same hue
              AppShell's own DOT_CLASS already uses for "running/live" (node_started). */}
          {newCount > 0 && (
            <button
              type="button"
              onClick={jumpToLatest}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-blue-300 bg-blue-50 px-3 py-1 text-xs font-medium text-blue-800 shadow-sm hover:bg-blue-100"
            >
              {newCount} new event{newCount === 1 ? "" : "s"} ↓
            </button>
          )}
        </div>
      )}
    </ViewContainer>
  );
}

function GroupRow({ tool, events, durations }: { tool: string; events: RunLogEvent[]; durations: Map<number, number> }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-neutral-50"
      >
        <span aria-hidden className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-neutral-300 bg-neutral-100 text-[9px] font-bold text-neutral-600">
          {tool.slice(0, 1).toUpperCase()}
        </span>
        <span className="font-medium text-neutral-800">
          {tool} × {events.length}
        </span>
        <span className="text-xs text-neutral-400">{expanded ? "collapse" : "expand"}</span>
      </button>
      {expanded && (
        <div className="flex flex-col divide-y divide-neutral-50 border-t border-neutral-100 pl-5">
          {events.map((e) => (
            <EventRow key={e.seq} event={e} duration={durations.get(e.seq)} />
          ))}
        </div>
      )}
    </div>
  );
}

/** Character-counted, not line-counted, unlike DiffView's own COLLAPSE_LINE_COUNT -- a "thinking"
 * block is frequently one long unbroken paragraph with no line structure to count. Same
 * truncate-and-expand shape DiffView.tsx already established (a length check, a "show more"
 * toggle, no unbounded height), just measured the way prose actually varies in size. */
const REASONING_COLLAPSE_CHARS = 480;

/** Full-width prose, never folded, quieter than a tool row (no dot, no group chrome, no click-to-
 * expand-detail -- the content is already fully on screen, exactly the inversion the Spec calls
 * the core lesson). Reads `payload.text` directly rather than `summary` (summary is only a
 * 160-char head, per both translators' own docstrings) -- falls back to summary for the
 * pathological case of a payload that didn't survive redaction/serialization as expected, so this
 * never renders blank. */
function ReasoningRow({ event }: { event: RunLogEvent }) {
  const [expanded, setExpanded] = useState(false);
  const payload = event.payload;
  const text = typeof payload?.text === "string" ? (payload.text as string) : (event.summary ?? "");
  // Claude's payload carries `kind: "thinking" | "text"` (claude_chat_model.py); Copilot's has no
  // such field (its one real narration shape, assistant.message_delta, doesn't distinguish the
  // two) -- shown when present, silently omitted otherwise, same fail-soft convention every other
  // payload-shape read in this file already follows.
  const kind = typeof payload?.kind === "string" ? (payload.kind as string) : null;
  const isLong = text.length > REASONING_COLLAPSE_CHARS;
  const visible = expanded || !isLong ? text : `${text.slice(0, REASONING_COLLAPSE_CHARS)}…`;

  return (
    <div className="bg-neutral-50/70 px-4 py-3">
      <div className="mb-1 flex items-center gap-2 text-[10px] font-medium uppercase tracking-wide text-neutral-400">
        <span>{kind === "thinking" ? "thinking" : "reasoning"}</span>
        {event.stage && <span>· {event.stage}</span>}
      </div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-neutral-800">{visible}</p>
      {isLong && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 text-xs font-medium text-neutral-500 hover:text-neutral-700"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** Minor 9 (Phase E audit): the data's already on the row (draft/audit/fix's NODE_FINISHED, per
 * LiveCostChip.tsx's own docstring on which events actually carry `token_usage`) -- this just
 * reads it. Honest-null rule: `cost` missing or explicitly null (Copilot's honest "the CLI didn't
 * report one" case, same as LiveCostChip's own doc note) returns null, not 0 -- a real "$0.00" chip
 * would claim something was reported that wasn't. Unlike LiveCostChip's run-total (2 decimals, a
 * sum large enough for cents to matter), a single real turn is frequently sub-cent (fix-e3a-report's
 * own disclosed real calls: $0.0062, $0.0217) -- 4 decimals here so a genuine tiny cost doesn't
 * round down to the exact same "$0.00" the null case must never show. */
function turnCost(event: RunLogEvent): number | null {
  const raw = event.token_usage?.cost;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function EventRow({ event, duration }: { event: RunLogEvent; duration?: number }) {
  const [expanded, setExpanded] = useState(false);
  const tool = toolNameOf(event);
  const arg = tool ? argSummary(event.payload) : null;
  const hasDetail = event.payload != null && Object.keys(event.payload).length > 0;
  const cost = turnCost(event);

  return (
    <div>
      <button
        type="button"
        disabled={!hasDetail}
        onClick={() => setExpanded((v) => !v)}
        className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm ${hasDetail ? "hover:bg-neutral-50" : "cursor-default disabled:opacity-100"}`}
      >
        {tool ? (
          <span aria-hidden className="flex h-4 w-4 shrink-0 items-center justify-center rounded border border-neutral-300 bg-neutral-100 text-[9px] font-bold text-neutral-600">
            {tool.slice(0, 1).toUpperCase()}
          </span>
        ) : (
          <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${TYPE_DOT[event.type]}`} />
        )}
        <span className="text-neutral-700">{event.summary ?? event.type}</span>
        {arg && <span className="truncate font-mono text-xs text-neutral-400">{arg}</span>}
        {event.stage && <span className="ml-auto shrink-0 text-xs text-neutral-400">{event.stage}</span>}
        {duration != null && <span className="shrink-0 text-xs text-neutral-400">{formatDuration(duration)}</span>}
        {cost != null && <Chip label="Cost" value={`$${cost.toFixed(4)}`} tone="gray" title="LLM spend for this turn" />}
      </button>
      {expanded && hasDetail && <EventDetail event={event} />}
    </div>
  );
}

function EventDetail({ event }: { event: RunLogEvent }) {
  const payload = event.payload;
  if (event.type !== "tool_call" || payload == null) {
    return (
      <pre className="max-h-48 overflow-auto border-t border-neutral-100 bg-neutral-50 p-3 text-xs">
        {JSON.stringify(payload, null, 2)}
      </pre>
    );
  }
  return <ToolCallDetail payload={payload} />;
}

/** Common-prefix/suffix-trimmed pseudo-diff for an Edit-tool `old_string`/`new_string` pair. No
 * real diff-computation library is safely available here (see DiffView.tsx's module docstring for
 * why not -- the only `diff`/jsdiff path in node_modules is an incidental transitive dependency of
 * a test runner, not something safe to import from app code), so this isn't a full line-level diff
 * -- no shared-context detection beyond the two ends, no moved/reordered-line tracking. It's
 * strictly better than marking the entire block changed for the dominant real case though: a
 * small, localized edit inside an otherwise-unchanged block, trimming the unchanged lines off both
 * ends down to real unified-diff context lines (leading space) and marking only the differing
 * middle chunk removed/added.
 * ponytail: prefix/suffix trim, not LCS -- swap for a real diff lib (e.g. jsdiff) if a caller ever
 * needs moved-line-aware diffing. */
function buildTrimmedDiff(oldStr: string, newStr: string): string {
  const oldLines = oldStr.split("\n");
  const newLines = newStr.split("\n");

  const maxPrefix = Math.min(oldLines.length, newLines.length);
  let prefix = 0;
  while (prefix < maxPrefix && oldLines[prefix] === newLines[prefix]) prefix++;

  const maxSuffix = maxPrefix - prefix; // bounds the scan so prefix+suffix can never overlap
  let suffix = 0;
  while (suffix < maxSuffix && oldLines[oldLines.length - 1 - suffix] === newLines[newLines.length - 1 - suffix]) {
    suffix++;
  }

  const contextBefore = oldLines.slice(0, prefix);
  const removed = oldLines.slice(prefix, oldLines.length - suffix);
  const added = newLines.slice(prefix, newLines.length - suffix);
  const contextAfter = oldLines.slice(oldLines.length - suffix);

  return [
    ...contextBefore.map((l) => ` ${l}`),
    ...removed.map((l) => `-${l}`),
    ...added.map((l) => `+${l}`),
    ...contextAfter.map((l) => ` ${l}`),
  ].join("\n");
}

/** Renders EITHER real captured shape: Claude's correlated `{name, input, result, is_error}`
 * (claude_chat_model.py) or Copilot's uncorrelated raw `data` dict with no confirmed `input`/
 * `result` keys at all (copilot_chat_model.py) -- the generic JSON fallback at the end is what
 * keeps the latter from rendering as an empty panel. */
function ToolCallDetail({ payload }: { payload: Record<string, unknown> }) {
  const input = payload.input;
  const inputObj = input && typeof input === "object" ? (input as Record<string, unknown>) : null;
  const oldStr = typeof inputObj?.old_string === "string" ? (inputObj.old_string as string) : null;
  const newStr = typeof inputObj?.new_string === "string" ? (inputObj.new_string as string) : null;
  const result = typeof payload.result === "string" ? payload.result : payload.result != null ? JSON.stringify(payload.result, null, 2) : null;
  const isError = payload.is_error === true;

  // Neither a recognized Claude-shaped `input`/`result` -- Copilot's uncorrelated shape, or an
  // unpaired call_start/call_end fragment. Dump the raw (already-redacted) payload so something
  // useful still shows instead of an empty panel.
  if (input == null && result == null) {
    return (
      <pre className="max-h-48 overflow-auto border-t border-neutral-100 bg-neutral-50 p-3 text-xs">
        {JSON.stringify(payload, null, 2)}
      </pre>
    );
  }

  return (
    <div className="space-y-2 border-t border-neutral-100 bg-neutral-50 p-3">
      {oldStr != null && newStr != null ? (
        // buildTrimmedDiff (see its own docstring): a common-prefix/suffix-trimmed pseudo-diff,
        // not a full line-level one, but real DiffView rendering real redacted Edit-tool content
        // end to end.
        <DiffView
          title={typeof inputObj?.file_path === "string" ? (inputObj.file_path as string) : undefined}
          diff={buildTrimmedDiff(oldStr, newStr)}
        />
      ) : (
        input != null && (
          <div>
            <div className="mb-1 text-xs font-medium text-neutral-500">Input</div>
            <pre className="max-h-48 overflow-auto rounded-lg border border-neutral-200 bg-white p-2 text-xs">
              {typeof input === "string" ? input : JSON.stringify(input, null, 2)}
            </pre>
          </div>
        )
      )}
      {result != null && (
        <div>
          <div className="mb-1 flex items-center gap-2 text-xs font-medium text-neutral-500">
            <span>Result</span>
            {isError && <Chip label="error" value="" tone="red" />}
          </div>
          {looksLikeDiff(result) ? <DiffView diff={result} /> : (
            <pre className="max-h-48 overflow-auto rounded-lg border border-neutral-200 bg-white p-2 text-xs">{result}</pre>
          )}
        </div>
      )}
    </div>
  );
}
