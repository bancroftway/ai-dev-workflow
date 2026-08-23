"use client";

import { useEffect, useState } from "react";
import { useAgent } from "@copilotkit/react-core/v2";
import { Chip } from "@/components/MetricsBar";
import { DiffView, looksLikeDiff } from "@/components/DiffView";
import { ViewContainer } from "@/components/ViewContainer";
import { useWorkflowThread } from "@/lib/workflow-thread-context";

/**
 * Folding tool-call-row event log -- Part 2 Task 8. Standalone/reusable on purpose (brief's own
 * instruction): Task 13 decides which tab this lives in, this component only needs a session_id
 * to render itself anywhere it's mounted.
 *
 * Data comes from BOTH of Task 1/2's real destinations for the same underlying event, merged by
 * `seq` (the durable store's own dedup key -- an event is only ever live-dispatched AFTER
 * run_event_store.append_event has already assigned it one, see run_event_stream.py's docstring):
 *  - `GET /sessions/{session_id}/events` (this task's new backend route) -- history-so-far,
 *    fetched once per session_id. Covers a finished run and a fresh page load/reconnect, since...
 *  - ...the live AG-UI CUSTOM event channel (Task 2) only fires while THIS browser tab is
 *    subscribed to an agent that is actively streaming a run. `agent.subscribe({onCustomEvent})`
 *    is the documented, always-safe way to observe it (see useAgent's own JSDoc in
 *    @copilotkit/react-core/v2 -- "calling agent.subscribe(...) ... is always safe").
 * No polling: a mount-time fetch plus a live subscription for the rest of this tab's lifetime
 * already covers every real case (opened before a run starts, opened mid-run, opened after the
 * run finished) without inventing a refresh loop nothing asked for.
 */

interface RunLogEvent {
  seq: number;
  run_id: string;
  session_id: string;
  ts: string;
  stage: string | null;
  node: string | null;
  type: "node_started" | "node_finished" | "tool_call" | "reasoning" | "gate_paused" | "gate_resolved";
  summary: string | null;
  payload: Record<string, unknown> | null;
  token_usage: Record<string, unknown> | null;
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

function mergeEvents(prev: RunLogEvent[], incoming: RunLogEvent[]): RunLogEvent[] {
  const bySeq = new Map(prev.map((e) => [e.seq, e]));
  for (const e of incoming) bySeq.set(e.seq, e);
  return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}

/** Both providers build a TOOL_CALL event's `summary` as literally `tool call: {name}`
 * (claude_chat_model.py / copilot_chat_model.py's own `_translate_intermediate_events`) -- reading
 * the tool name back out of `summary` works identically for either provider's payload shape,
 * unlike reading a `name`/`toolName`/`tool_name` key off `payload` directly (Claude's is `name`;
 * Copilot's is unconfirmed and may not exist at all, see that module's own docstring). */
function toolNameOf(e: RunLogEvent): string | null {
  if (e.type !== "tool_call") return null;
  const m = e.summary?.match(/^tool call: (.+)$/);
  return m ? m[1] : "tool";
}

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
 * precedes it -- no real call site emits NODE_STARTED yet (graph.py only ever emits
 * NODE_FINISHED today), so this never fires against current real data, but the type has carried
 * NODE_STARTED since Task 1 specifically so a later call site can add it without a schema change;
 * this is that "duration if available" consumer already being ready for it. */
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

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
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
  const { threadId, localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const [events, setEvents] = useState<RunLogEvent[]>([]);

  useEffect(() => {
    // No synchronous setEvents([]) reset here (react-hooks/set-state-in-effect) -- threadId is
    // effectively stable for this component's mounted lifetime, the same assumption every sibling
    // view (MetricsBar, SessionOverview, ...) already makes via useWorkflowThread/useAgent: a
    // session switch is a full Next.js route navigation (a different [sessionId] param), which
    // remounts this subtree, not an in-place threadId prop change.
    let cancelled = false;
    fetch(`/api/sessions/${encodeURIComponent(threadId)}/events`)
      .then((r) => (r.ok ? (r.json() as Promise<{ events: RunLogEvent[] }>) : null))
      .then((data) => {
        if (!cancelled && data) setEvents((prev) => mergeEvents(prev, data.events));
      })
      .catch(() => {
        // History fetch is a best-effort fallback (see module docstring) -- the live subscription
        // below still works even if this request fails (offline history, transient 5xx, ...).
      });
    return () => {
      cancelled = true;
    };
  }, [threadId]);

  useEffect(() => {
    const { unsubscribe } = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name !== "run_event") return;
        try {
          const raw: unknown = event.value;
          const value = (typeof raw === "string" ? JSON.parse(raw) : raw) as RunLogEvent;
          setEvents((prev) => mergeEvents(prev, [value]));
        } catch {
          // Malformed live payload -- ignore; the durable store already has the real row and a
          // future refetch/reconnect will pick it up.
        }
      },
    });
    return unsubscribe;
  }, [agent]);

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
