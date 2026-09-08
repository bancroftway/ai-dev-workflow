"use client";

import { useEffect, useState } from "react";
import { useAgent } from "@copilotkit/react-core/v2";
import { useWorkflowThread } from "@/lib/workflow-thread-context";

/**
 * Shared real-event-stream plumbing for every run-visibility view (EventLogView.tsx's Task 8,
 * Swimlane.tsx's Task 9, ...). Factored out of EventLogView.tsx when Task 9 needed the identical
 * data for a second component -- per that task's own explicit instruction, this is reused rather
 * than stood up a second time: an SSE connection to `GET /sessions/{id}/events/stream` (sends
 * this session's full history on connect, then pushes new rows as they land -- covers a finished
 * run, a fresh page load/reconnect, AND a session a different process/tab is driving, e.g.
 * run_headless.py) merged with the live AG-UI CUSTOM `run_event` channel (Task 2, only fires while
 * THIS tab is itself the one actively streaming a run, but delivers with near-zero latency when it
 * applies), deduped by `seq` (the durable store's own dedup key -- an event is only ever
 * live-dispatched AFTER run_event_store.append_event has already assigned it one, see
 * run_event_stream.py's docstring). Both sources converge on the same seq-keyed store, so keeping
 * both is free -- the SSE stream is the one that actually covers every case, the AG-UI channel is
 * just a latency shortcut for the common one.
 */

export interface RunLogEvent {
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

/** `RunLogEvent.ts` arrives as a real UTC instant (`dbo.run_events.ts` is SYSUTCDATETIME-assigned,
 * run_event_store.py) but serialized with NO "Z"/offset suffix (confirmed against the real
 * backend response: `"2026-08-23T22:26:03"`, not `"...03Z"` -- FastAPI's default encoding of a
 * naive-but-semantically-UTC `datetime`). Per the ECMAScript Date Time String spec, a date-TIME
 * string with no offset parses as the *browser's local* time zone, not UTC -- so a bare
 * `new Date(e.ts)` silently reads a UTC value as local, shifting every absolute clock reading by
 * the viewer's own UTC offset (0 on a UTC-local dev box, which is why this stayed unnoticed until
 * Swimlane.tsx -- Part 2 Task 9 -- became the first consumer to render an absolute clock label;
 * EventLogView's own `computeDurations` only ever subtracts two equally-shifted values, which
 * cancels the error). Appending "Z" before parsing (only when not already offset-qualified, so a
 * future backend change to include one doesn't get double-corrected) is the fix -- applied once
 * here rather than at each of the 6 call sites across EventLogView.tsx/Swimlane.tsx that parse a
 * `.ts` string. */
export function parseEventTs(ts: string): number {
  const qualified = /Z$|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`;
  return new Date(qualified).getTime();
}

function mergeEvents(prev: RunLogEvent[], incoming: RunLogEvent[]): RunLogEvent[] {
  // Fast path (the normal live-append case: one CUSTOM event per call): incoming is strictly
  // ascending AND entirely newer than prev's last seq, so a plain append is already deduped and
  // sorted -- no Map rebuild, no full re-sort per live event. Strict > against prev's last seq
  // rejects any seq already held; the same strict > within incoming rejects internal dupes and
  // out-of-order batches, which fall through to the Map+sort path below (history merges).
  let last = prev.length > 0 ? prev[prev.length - 1].seq : -Infinity;
  let appendable = true;
  for (const e of incoming) {
    if (e.seq > last) {
      last = e.seq;
    } else {
      appendable = false;
      break;
    }
  }
  if (appendable) return incoming.length === 0 ? prev : [...prev, ...incoming];
  const bySeq = new Map(prev.map((e) => [e.seq, e]));
  for (const e of incoming) bySeq.set(e.seq, e);
  return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}

/** Both providers build a TOOL_CALL event's `summary` as literally `tool call: {name}`
 * (claude_chat_model.py / copilot_chat_model.py's own `_translate_intermediate_events`) -- reading
 * the tool name back out of `summary` works identically for either provider's payload shape,
 * unlike reading a `name`/`toolName`/`tool_name` key off `payload` directly (Claude's is `name`;
 * Copilot's is unconfirmed and may not exist at all, see that module's own docstring). Shared here
 * (originally EventLogView.tsx-only) so Swimlane.tsx's tool-call lane uses the identical rule
 * rather than a second copy that could drift. */
export function toolNameOf(e: RunLogEvent): string | null {
  if (e.type !== "tool_call") return null;
  const m = e.summary?.match(/^tool call: (.+)$/);
  return m ? m[1] : "tool";
}

function truncateOneLine(s: string, max: number): string {
  const oneLine = s.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? `${oneLine.slice(0, max)}…` : oneLine;
}

/** One-line arg preview for a dense tool-call row (Agent Narration Drawer feature; recovered from
 * the deleted EventLogView.tsx's identical helper, git show 7a37340^:src/components/
 * EventLogView.tsx). Claude's shape wraps args in `payload.input`; Copilot's uncorrelated shape (no
 * confirmed real example yet -- copilot_chat_model.py's own docstring) has no such wrapper, so this
 * also tries a couple of plausible top-level keys directly on `payload` before giving up and
 * showing no arg summary at all -- never throws, never assumes either shape. */
export function argSummary(payload: Record<string, unknown> | null): string | null {
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

/** Absolute clock label for one event row (Agent Narration Drawer, user request 2026-09-06: tell
 * apart same-looking reasoning/tool-call lines from different stages/times at a glance). Includes
 * the date, not just time-of-day -- a run can genuinely span days (SessionOverview's own "stage 2
 * of 8 a day into a run" case), so a bare HH:MM:SS would misleadingly collide across days. Goes
 * through parseEventTs, not a bare `new Date(e.ts)`, for the same UTC-suffix reason that function's
 * own docstring documents. */
export function formatEventTimestamp(ts: string): string {
  return new Date(parseEventTs(ts)).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Human-readable duration, shared so a span reads identically in EventLogView's row detail and
 * Swimlane's bars/tooltips. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

/** Which stages have a node genuinely executing right now, straight from the live event stream --
 * NOT from `state.stages[key].status`, which only updates when the run pauses at a human gate
 * (user feedback 2026-09-01: a non-gated stage like ac-to-tests cycles through "ready_for_review"
 * between verify attempts -- a generic status name the backend reuses for "draft phase done"
 * regardless of whether a human is involved -- so a status-only check reads a stage that is
 * actively retrying as "awaiting", not "running", almost the entire time). Scoped to the latest
 * `run_id` so a node_started left open by a hard-killed agent process (several observed live) can
 * never read as "still running" forever -- that run is over, whether or not it got a matching
 * node_finished. First consumer: SessionOverview's per-stage table; second: AppShell's tab pills.
 *
 * `runActive` (Workflow Liveness Fix) is a tri-state backstop, not a plain boolean: an explicit
 * `false` (the backend's run_activity refcount says nothing is attached to this session right
 * now) always wins and returns an empty set -- this is what actually fixes a hard-killed run's
 * dangling node_started, which the latest-run-id scoping above can still misread as running (the
 * old run IS the latest run_id until a new one emits its first event). `null`/`undefined` (the
 * caller's run-activity context hasn't loaded yet -- always true on first render) must NOT be
 * treated as `false`: that would hide a genuinely-running session's spinner for a tick on every
 * page load, a regression this fix must not introduce. Only omit the argument (or pass `true`) to
 * keep the old, ungated behavior. */
export const NODE_PHASE_LABEL: Record<string, string> = {
  draft: "Drafting",
  audit: "Auditing",
  verify: "Verifying",
  fix: "Fixing",
};

// Stage -> currently-open node (or none). Newest node_started per stage wins if a retry leaves an
// earlier node's span dangling (no matching node_finished, e.g. an infra-exhausted attempt); a
// node_finished only closes the entry if it matches the currently-open node, so a late/stale
// finished event for an already-superseded node can't wrongly clear the real one.
export function computeRunningPhases(events: RunLogEvent[], runActive?: boolean | null): Map<string, string> {
  if (runActive === false) return new Map();
  const latestRunId = events.length > 0 ? events[events.length - 1].run_id : null;
  const openByStage = new Map<string, string>();
  for (const e of events) {
    if (!e.stage || !e.node || e.run_id !== latestRunId) continue;
    if (e.type === "node_started") openByStage.set(e.stage, e.node);
    else if (e.type === "node_finished" && openByStage.get(e.stage) === e.node) openByStage.delete(e.stage);
  }
  return openByStage;
}

export function computeRunningStages(events: RunLogEvent[], runActive?: boolean | null): Set<string> {
  return new Set(computeRunningPhases(events, runActive).keys());
}

interface StreamState {
  events: RunLogEvent[];
  listeners: Set<(events: RunLogEvent[]) => void>;
  source?: EventSource;
}

// Keyed by threadId, module-level (outside React) so every useRunEvents() caller mounted for the
// same session shares one EventSource instead of each opening its own -- AppShell, BuildView,
// LiveCostChip and SessionOverview all call this hook independently, and one connection per tab
// (not per component) is what actually matters here (same sharing reasoning as the old poll timer
// this replaced, from a 2026-09-02 investigation that found 4 unsynchronized timers per tab).
// First subscriber for a threadId opens the connection; each additional one just registers and
// gets the current + all future events for free; last one to unmount closes it.
const streamStates = new Map<string, StreamState>();

function openEventStream(threadId: string, s: StreamState): void {
  const source = new EventSource(`/api/sessions/${encodeURIComponent(threadId)}/events/stream`);
  source.addEventListener("run_event", (ev) => {
    try {
      const value = JSON.parse((ev as MessageEvent).data) as RunLogEvent;
      s.events = mergeEvents(s.events, [value]);
      s.listeners.forEach((l) => l(s.events));
    } catch {
      // Malformed live payload -- ignore; the durable store already has the real row and a
      // future event/reconnect will pick it up.
    }
  });
  // 'done'/'not_found' mean the session has reached a real terminal state (or is gone) -- close
  // explicitly so the browser's default auto-reconnect-on-close behavior doesn't keep re-opening
  // it forever. 'reconnect' is different: the agent's own _SSE_MAX_CONNECTION_SECONDS cap
  // (stream_session_events' own comment) -- the session can still be very much in_progress when
  // THAT fires, so it means "open a fresh connection," not "stop." Treating it like 'done' would
  // silently go quiet on every run that outlives one connection's lifetime. Plain network blips
  // get no special handling here: that's exactly the case EventSource's own built-in
  // reconnect-on-error already covers.
  source.addEventListener("done", () => source.close());
  source.addEventListener("not_found", () => source.close());
  source.addEventListener("reconnect", () => {
    source.close();
    openEventStream(threadId, s);
  });
  s.source = source;
}

function subscribeToEventStream(threadId: string, onEvents: (events: RunLogEvent[]) => void): () => void {
  let state = streamStates.get(threadId);
  if (!state) {
    const s: StreamState = { events: [] as RunLogEvent[], listeners: new Set() };
    openEventStream(threadId, s);
    streamStates.set(threadId, s);
    state = s;
  }
  state.listeners.add(onEvents);
  onEvents(state.events);
  return () => {
    state!.listeners.delete(onEvents);
    if (state!.listeners.size === 0) {
      state!.source?.close();
      streamStates.delete(threadId);
    }
  };
}

/** This session's full event history, oldest first, live-updating for as long as the caller stays
 * mounted. Reads threadId/localAgentId from useWorkflowThread() internally -- same assumption
 * EventLogView's original effect made: a session switch is a full Next.js route navigation, which
 * remounts the caller, not an in-place threadId prop change, so no reset-on-change handling is
 * needed here. */
export function useRunEvents(): RunLogEvent[] {
  const { threadId, localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const [events, setEvents] = useState<RunLogEvent[]>([]);

  useEffect(
    () => subscribeToEventStream(threadId, (streamed) => setEvents((prev) => mergeEvents(prev, streamed))),
    [threadId],
  );

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

  return events;
}
