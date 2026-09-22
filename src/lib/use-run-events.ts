"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
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
 *
 * Memory fix (2026-09-22): every mounted consumer of this module used to hold its OWN full-array
 * copy (a per-component useState fed by mergeEvents) on top of the module-level StreamState that
 * already held the canonical array -- N+1 copies of a session's entire event history for N
 * mounted components. Checking every actual consumer found each one wants exactly one of two
 * disjoint slices, never the raw union: AppShell/BuildView/QualityView/LiveCostChip/
 * SessionOverview only ever read `node_started`/`node_finished`/`gate_paused`/`gate_resolved`
 * events (STRUCTURAL_TYPES below); AgentNarrationDrawer is the only consumer of `tool_call`/
 * `reasoning` events (NARRATION_TYPES), and it already filtered the full array down to them
 * itself. Both filtered arrays are now maintained once, at the shared-store level
 * (`applyMerge` below), and every consumer subscribes via `useSyncExternalStore` to a shared
 * reference instead of copying anything -- `useStructuralRunEvents`/`useNarrationRunEvents`
 * below. `useRunEvents` (the full array) is kept only as a general escape hatch; nothing in this
 * codebase needs it once its former callers are swapped to the narrower hook.
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
  // Overview-tab redraft history (Workstream 3): byte sizes only -- sessions_api.py's
  // RunEventResponse deliberately never carries the full input/output text here (see that
  // model's own comment); SessionOverview's redraft-history column fetches text on demand, by
  // seq, only when a user actually hovers a row.
  input_size: number | null;
  output_size: number | null;
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

interface MergeResult {
  events: RunLogEvent[];
  // The new tail, when `incoming` was a pure in-order append (the common case): lets callers
  // extend a derived filtered array in O(new) instead of re-filtering the whole thing. `null`
  // means a full rebuild happened (dedup/reorder, e.g. the AG-UI channel redelivering something
  // the SSE channel already has) -- callers must re-derive from `events` in that case, same cost
  // profile this rare path already had before this split.
  appended: RunLogEvent[] | null;
}

function mergeEvents(prev: RunLogEvent[], incoming: RunLogEvent[]): MergeResult {
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
  if (appendable) {
    if (incoming.length === 0) return { events: prev, appended: [] };
    return { events: [...prev, ...incoming], appended: incoming };
  }
  const bySeq = new Map(prev.map((e) => [e.seq, e]));
  for (const e of incoming) bySeq.set(e.seq, e);
  return { events: Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq), appended: null };
}

const STRUCTURAL_TYPES = new Set<RunLogEvent["type"]>(["node_started", "node_finished", "gate_paused", "gate_resolved"]);
const NARRATION_TYPES = new Set<RunLogEvent["type"]>(["tool_call", "reasoning"]);

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
 * node_finished. Takes a STRUCTURAL-only event array (useStructuralRunEvents below) -- it only
 * ever reads node_started/node_finished, so this needs no change of its own for that split.
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
  structuralEvents: RunLogEvent[];
  narrationEvents: RunLogEvent[];
  listeners: Set<() => void>;
  source?: EventSource;
}

// Keyed by threadId, module-level (outside React) so every consumer mounted for the same session
// shares one EventSource instead of each opening its own -- AppShell, BuildView, QualityView,
// LiveCostChip, SessionOverview and AgentNarrationDrawer all call one of this module's hooks
// independently, and one connection per tab (not per component) is what actually matters here
// (same sharing reasoning as the old poll timer this replaced, from a 2026-09-02 investigation
// that found 4 unsynchronized timers per tab).
// First subscriber for a threadId opens the connection; each additional one just registers and
// gets the current + all future events for free; last one to unmount closes it.
const streamStates = new Map<string, StreamState>();

function getOrCreateState(threadId: string): StreamState {
  let s = streamStates.get(threadId);
  if (!s) {
    s = { events: [], structuralEvents: [], narrationEvents: [], listeners: new Set() };
    streamStates.set(threadId, s);
  }
  return s;
}

// Referentially stable -- passed to useSyncExternalStore's getServerSnapshot below, which React
// requires be the same reference every call or it re-renders forever. There is no server-side
// event data (this is all live-connection state, module-level, browser-only), so every hook's
// server-rendered pass is simply empty, same as the pre-split code's `useState<RunLogEvent[]>([])`
// initial value before its first effect ran.
const EMPTY_EVENTS: RunLogEvent[] = [];

/** Merges `incoming` into `s` (canonical array + both filtered views) and notifies listeners.
 * Shared by both live paths (SSE `run_event` messages and the AG-UI CUSTOM channel) so a
 * consumer sees the identical shape regardless of which one actually delivered a given row --
 * redundant delivery of the same seq via both channels is a harmless no-op merge, not a bug, so
 * every mounted hook instance's own AG-UI subscription can push into this SAME shared state
 * without any "only one subscriber does it" coordination. */
function applyMerge(s: StreamState, incoming: RunLogEvent[]): void {
  const before = s.events;
  const { events, appended } = mergeEvents(before, incoming);
  if (events === before) return;
  s.events = events;
  if (appended) {
    if (appended.length > 0) {
      const newStructural = appended.filter((e) => STRUCTURAL_TYPES.has(e.type));
      const newNarration = appended.filter((e) => NARRATION_TYPES.has(e.type));
      if (newStructural.length > 0) s.structuralEvents = [...s.structuralEvents, ...newStructural];
      if (newNarration.length > 0) s.narrationEvents = [...s.narrationEvents, ...newNarration];
    }
  } else {
    // Slow path (dedup/reorder): same full-rebuild cost mergeEvents' own slow path already pays.
    s.structuralEvents = events.filter((e) => STRUCTURAL_TYPES.has(e.type));
    s.narrationEvents = events.filter((e) => NARRATION_TYPES.has(e.type));
  }
  s.listeners.forEach((l) => l());
}

function openEventStream(threadId: string, s: StreamState): void {
  const source = new EventSource(`/api/sessions/${encodeURIComponent(threadId)}/events/stream`);
  source.addEventListener("run_event", (ev) => {
    try {
      const value = JSON.parse((ev as MessageEvent).data) as RunLogEvent;
      applyMerge(s, [value]);
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

function subscribe(threadId: string, onStoreChange: () => void): () => void {
  const s = getOrCreateState(threadId);
  if (!s.source) openEventStream(threadId, s);
  s.listeners.add(onStoreChange);
  return () => {
    s.listeners.delete(onStoreChange);
    if (s.listeners.size === 0) {
      s.source?.close();
      streamStates.delete(threadId);
    }
  };
}

/** Shared plumbing for all three exported hooks below: subscribes to this thread's module-level
 * StreamState via useSyncExternalStore (one shared array reference per view, no per-component
 * copy) and wires up the AG-UI low-latency channel to push into that same shared state. `select`
 * picks which of the state's three arrays this hook returns -- it must be referentially stable
 * across calls that didn't change that array (useSyncExternalStore compares via Object.is), which
 * `applyMerge` guarantees (a view's array reference only ever changes when something matching
 * that view actually arrived). */
function useSharedRunEvents(select: (s: StreamState) => RunLogEvent[]): RunLogEvent[] {
  const { threadId, localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });

  // `subscribe` must stay referentially stable across renders for the same threadId --
  // useSyncExternalStore re-subscribes (tearing down and re-running the callback below)
  // whenever this function's IDENTITY changes, not just when threadId itself changes. An inline
  // closure here would recreate on every render, and since the last listener leaving triggers
  // real teardown (closing the EventSource, `subscribe`'s own cleanup below), that churn would
  // close and reopen the connection on every single render for a lone subscriber -- exactly the
  // "one connection per tab" design this module exists to guarantee, defeated by omission.
  const subscribeToThisThread = useCallback(
    (onStoreChange: () => void) => subscribe(threadId, onStoreChange),
    [threadId],
  );

  const events = useSyncExternalStore(
    subscribeToThisThread,
    () => select(getOrCreateState(threadId)),
    () => EMPTY_EVENTS,
  );

  useEffect(() => {
    const { unsubscribe } = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name !== "run_event") return;
        try {
          const raw: unknown = event.value;
          const value = (typeof raw === "string" ? JSON.parse(raw) : raw) as RunLogEvent;
          applyMerge(getOrCreateState(threadId), [value]);
        } catch {
          // Malformed live payload -- ignore; the durable store already has the real row and a
          // future refetch/reconnect will pick it up.
        }
      },
    });
    return unsubscribe;
  }, [agent, threadId]);

  return events;
}

/** This session's full event history, oldest first, live-updating for as long as the caller stays
 * mounted. Reads threadId/localAgentId from useWorkflowThread() internally -- same assumption
 * EventLogView's original effect made: a session switch is a full Next.js route navigation, which
 * remounts the caller, not an in-place threadId prop change, so no reset-on-change handling is
 * needed here. Kept as a general escape hatch -- every current consumer needs only one of the two
 * narrower views below, and is wired to that one instead. */
export function useRunEvents(): RunLogEvent[] {
  return useSharedRunEvents((s) => s.events);
}

/** `node_started`/`node_finished`/`gate_paused`/`gate_resolved` events only -- what
 * AppShell/BuildView/QualityView/LiveCostChip/SessionOverview actually read (computeRunningPhases/
 * computeRunningStages, or a `token_usage` check -- token_usage never appears on a tool_call/
 * reasoning event). A long session's event count is dominated by tool_call/reasoning volume (a
 * single LLM turn issues dozens of tool calls; a node only ever gets one start/finish pair), so
 * this is a substantially smaller array than the full stream for every one of these consumers. */
export function useStructuralRunEvents(): RunLogEvent[] {
  return useSharedRunEvents((s) => s.structuralEvents);
}

/** `tool_call`/`reasoning` events only -- AgentNarrationDrawer's own narration feed. Previously
 * that component filtered the full array down to this itself, on every mount, after holding a
 * full copy just to discard most of it; the filtering now happens once, here, shared across
 * however many times the drawer is mounted/unmounted in a session. */
export function useNarrationRunEvents(): RunLogEvent[] {
  return useSharedRunEvents((s) => s.narrationEvents);
}

/** One normalized stage/rebuild-placement key's server-computed duration+cost (mirrors
 * agent/src/sessions_api.py's StageSummaryEntry, already parsed to plain numbers -- see
 * useSessionSummary below). */
export interface StageSummary {
  key: string;
  first: number;
  last: number;
  cost: number;
  costKnown: boolean;
}

interface RawStageSummaryEntry {
  key: string;
  first_ts: string;
  last_ts: string;
  cost: number;
  cost_known: boolean;
}

// How often SessionOverview re-polls the summary endpoint while a run is active. Deliberately
// coarser than the backend's own SSE poll loop (sessions_api.py's _SSE_EVENTS_POLL_SECONDS=2) --
// this is a derived/secondary read (duration/cost numbers), not the primary live event feed.
const SESSION_SUMMARY_POLL_MS = 5000;

/** Overview-tab fix (2026-09-22): server-computed per-stage/per-rebuild-placement duration+cost,
 * fetched from `GET /api/sessions/{threadId}/events/summary` (proxying sessions_api.py's
 * get_session_summary, which itself calls agent/src/run_event_summary.py) instead of
 * SessionOverview re-deriving these two numbers from its own held event array on every render.
 * `runActive` is taken as a parameter -- the caller already holds it via useRunActivity() -- so
 * this hook stays a plain fetch/poll utility, not a second consumer of that context. Fetches once
 * on mount, then re-polls only while `runActive === true`; a finished/idle session fetches once
 * and stops. Fails soft on a failed fetch (keeps the last-known map) -- matches fetchEventIo's
 * own null-on-failure convention and the backend's fail-soft append_event/emit_live pattern; this
 * must never throw into the caller's render.
 *
 * Scope note: unlike useStructuralRunEvents/useNarrationRunEvents above (one shared snapshot via
 * useSyncExternalStore), this hook polls independently per mounted instance -- acceptable since
 * SessionOverview is its only consumer today; a second consumer would need the same
 * module-level-dedup treatment those two hooks already have. */
export function useSessionSummary(threadId: string, runActive: boolean | null | undefined): Map<string, StageSummary> {
  const [summary, setSummary] = useState<Map<string, StageSummary>>(new Map());

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(threadId)}/events/summary`);
        if (!res.ok || cancelled) return;
        const data = (await res.json()) as { stages: RawStageSummaryEntry[] };
        if (cancelled) return;
        setSummary(
          new Map(
            data.stages.map((entry) => [
              entry.key,
              {
                key: entry.key,
                first: parseEventTs(entry.first_ts),
                last: parseEventTs(entry.last_ts),
                cost: entry.cost,
                costKnown: entry.cost_known,
              },
            ]),
          ),
        );
      } catch {
        // Fail soft -- keep whatever the last successful poll produced.
      }
    }

    void poll();
    if (runActive !== true) return () => {
      cancelled = true;
    };
    const interval = setInterval(() => void poll(), SESSION_SUMMARY_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [threadId, runActive]);

  return summary;
}
