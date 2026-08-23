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

function mergeEvents(prev: RunLogEvent[], incoming: RunLogEvent[]): RunLogEvent[] {
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

/** Human-readable duration, shared so a span reads identically in EventLogView's row detail and
 * Swimlane's bars/tooltips. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
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

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/sessions/${encodeURIComponent(threadId)}/events`)
      .then((r) => (r.ok ? (r.json() as Promise<{ events: RunLogEvent[] }>) : null))
      .then((data) => {
        if (!cancelled && data) setEvents((prev) => mergeEvents(prev, data.events));
      })
      .catch(() => {
        // History fetch is a best-effort fallback -- the live subscription below still works even
        // if this request fails (offline history, transient 5xx, ...).
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

  return events;
}
