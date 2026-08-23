"use client";

import { useState } from "react";
import { Chip } from "@/components/MetricsBar";
import { DiffView, looksLikeDiff } from "@/components/DiffView";
import { ViewContainer } from "@/components/ViewContainer";
import { formatDuration, toolNameOf, useRunEvents, type RunLogEvent } from "@/lib/use-run-events";

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
 */

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
        const ms = new Date(e.ts).getTime() - new Date(start.ts).getTime();
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

export function EventLogView() {
  const events = useRunEvents();
  const durations = computeDurations(events);
  const runs = groupConsecutive(events);

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Event Log</h1>
        <p className="text-sm text-neutral-500">Every node, tool call, and gate captured for this run, oldest first.</p>
      </div>
      {events.length === 0 ? (
        <p className="text-sm text-neutral-400">No events yet.</p>
      ) : (
        <div className="flex flex-col divide-y divide-neutral-100 rounded-lg border border-neutral-200">
          {runs.map((run) =>
            run.tool && run.events.length > 1 ? (
              <GroupRow key={run.events[0].seq} tool={run.tool} events={run.events} durations={durations} />
            ) : (
              <EventRow key={run.events[0].seq} event={run.events[0]} duration={durations.get(run.events[0].seq)} />
            ),
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

function EventRow({ event, duration }: { event: RunLogEvent; duration?: number }) {
  const [expanded, setExpanded] = useState(false);
  const tool = toolNameOf(event);
  const arg = tool ? argSummary(event.payload) : null;
  const hasDetail = event.payload != null && Object.keys(event.payload).length > 0;

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
