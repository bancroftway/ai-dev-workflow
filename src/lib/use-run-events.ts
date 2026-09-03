"use client";

import { useEffect, useState } from "react";
import { useAgent } from "@copilotkit/react-core/v2";
import { useWorkflowThread } from "@/lib/workflow-thread-context";

/**
 * Shared real-event-stream plumbing for every run-visibility view (EventLogView.tsx's Task 8,
 * Swimlane.tsx's Task 9, ...). Factored out of EventLogView.tsx when Task 9 needed the identical
 * data for a second component -- per that task's own explicit instruction, this is reused rather
 * than stood up a second time: a mount-time fetch of `GET /sessions/{id}/events` (history-so-far,
 * covers a finished run and a fresh page load/reconnect) merged with the live AG-UI CUSTOM
 * `run_event` channel (Task 2, only fires while this tab is actively watching a run) for the rest
 * of this component's mounted lifetime, deduped by `seq` (the durable store's own dedup key -- an
 * event is only ever live-dispatched AFTER run_event_store.append_event has already assigned it
 * one, see run_event_stream.py's docstring). No polling -- those two sources cover every real case.
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
export function computeRunningStages(events: RunLogEvent[], runActive?: boolean | null): Set<string> {
  if (runActive === false) return new Set();
  const latestRunId = events.length > 0 ? events[events.length - 1].run_id : null;
  const openNodeStage = new Map<string, string>(); // "run_id|node" -> stage, while still unfinished
  for (const e of events) {
    if (!e.stage || !e.node || e.run_id !== latestRunId) continue;
    const key = `${e.run_id}|${e.node}`;
    if (e.type === "node_started") openNodeStage.set(key, e.stage);
    else if (e.type === "node_finished") openNodeStage.delete(key);
  }
  return new Set(openNodeStage.values());
}

const POLL_MS = 15000;

interface PollState {
  events: RunLogEvent[];
  listeners: Set<(events: RunLogEvent[]) => void>;
  timer?: ReturnType<typeof setInterval>;
}

// Keyed by threadId, module-level (outside React) so every useRunEvents() caller mounted for the
// same session shares one fetch+timer loop instead of each running its own -- AppShell, BuildView,
// LiveCostChip and SessionOverview all call this hook independently, and until this were four
// unsynchronized 10s timers overlapping in phase, averaging a real request every 2-3s for one
// person looking at one session (2026-09-02 investigation). First subscriber for a threadId starts
// the loop; each additional one just registers and gets the current + all future events for free;
// last one to unmount tears it down.
const pollStates = new Map<string, PollState>();

function subscribeToPoll(threadId: string, onEvents: (events: RunLogEvent[]) => void): () => void {
  let state = pollStates.get(threadId);
  if (!state) {
    const s: PollState = { events: [], listeners: new Set() };
    const fetchOnce = () =>
      fetch(`/api/sessions/${encodeURIComponent(threadId)}/events`)
        .then((r) => (r.ok ? (r.json() as Promise<{ events: RunLogEvent[] }>) : null))
        .then((data) => {
          if (!data) return;
          s.events = mergeEvents(s.events, data.events);
          s.listeners.forEach((l) => l(s.events));
        })
        .catch(() => {
          // Best-effort -- the live subscription below still works even if one poll fails
          // (transient 5xx), and the next tick tries again.
        });
    fetchOnce();
    // Re-poll, not just the one mount-time fetch: `agent.subscribe` below only delivers events
    // for a run THIS tab's own agent instance is actively streaming -- a tab that reattaches to a
    // run already started elsewhere (Resume clicked from a different tab/reload, same known
    // mid-run reattach gap as state snapshots) never gets attached to that stream's custom events,
    // so its data would otherwise freeze at whatever existed at mount forever (user feedback
    // 2026-09-01: a stage's tab-pill spinner stayed on the wrong stage because of exactly this --
    // a direct fetch of this same endpoint had fresher data than the hook's own state). Separate
    // from, and slower than, AppShell.tsx's own 10s poll of the durable session row.
    s.timer = setInterval(fetchOnce, POLL_MS);
    pollStates.set(threadId, s);
    state = s;
  }
  state.listeners.add(onEvents);
  onEvents(state.events);
  return () => {
    state!.listeners.delete(onEvents);
    if (state!.listeners.size === 0) {
      clearInterval(state!.timer);
      pollStates.delete(threadId);
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
    () => subscribeToPoll(threadId, (polled) => setEvents((prev) => mergeEvents(prev, polled))),
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
