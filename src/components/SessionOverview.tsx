"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useMemo } from "react";
import { LiveCostChip } from "@/components/LiveCostChip";
import { RunningSpinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { computeRunningStages, formatDuration, parseEventTs, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { StageState, WorkflowState } from "@/lib/workflow-types";

const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started",
  drafting: "Drafting",
  needs_clarification: "Needs clarification",
  ready_for_review: "Ready for review",
  approved: "Approved",
};

/** The Overview tab: session cost + a per-stage table (duration, spend, redraft count, status).
 * Timeline (Swimlane.tsx) and the detailed event log (EventLogView.tsx) were removed here (user
 * request 2026-09-01) along with their now-dead component files (and DiffView.tsx, which existed
 * only to render EventLogView's diff payloads) -- this was the only place either was mounted. */

/** One stage's short human-facing note: the live failure feedback while it's failing, else what
 * the audit did, else the approved summary. Never the raw draft. */
function stageNote(stage: StageState): string | null {
  const v = stage.last_verification;
  if (v && !v.passed && stage.status !== "approved") return truncate(v.feedback, 140);
  if (stage.audit_findings?.length) return `${stage.audit_findings.length} audit finding(s) addressed`;
  const summary = (stage.approved_content as { summary?: string } | null)?.summary;
  if (stage.status === "approved" && summary) return truncate(summary, 140);
  return null;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// Shared between the header row and every stage row so the columns actually line up like a table
// (user feedback 2026-09-01) instead of each row's flex layout drifting with its own content width.
const ROW_GRID = "grid grid-cols-[1fr_4.5rem_4rem_5rem_9rem] items-center gap-3";


export function SessionOverview() {
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { copilotkit } = useCopilotKit();
  const state = (agent.state ?? {}) as WorkflowState;
  const stages = Object.entries(state.stages ?? {});
  const failure = state.run_failure;

  // Per-stage wall-clock + spend from the durable event stream, plus a redraft/rejection count:
  // a gate_resolved event with payload.decision === "rejected" is exactly a human rejection that
  // sent the stage back to its own draft node (make_gate_node, graph.py) -- a count Overview never
  // surfaced before (user feedback 2026-09-01).
  const events = useRunEvents();
  const perStage = useMemo(() => {
    const byStage = new Map<
      string,
      { first: number; last: number; cost: number; sawCost: boolean; rejections: number; running: boolean }
    >();
    for (const e of events) {
      if (!e.stage) continue;
      const ts = parseEventTs(e.ts);
      const entry =
        byStage.get(e.stage) ?? { first: ts, last: ts, cost: 0, sawCost: false, rejections: 0, running: false };
      entry.first = Math.min(entry.first, ts);
      entry.last = Math.max(entry.last, ts);
      const cost = Number((e.token_usage as { cost?: unknown } | null)?.cost);
      if (Number.isFinite(cost)) {
        entry.cost += cost;
        entry.sawCost = true;
      }
      if (e.type === "gate_resolved" && (e.payload as { decision?: string } | null)?.decision === "rejected") {
        entry.rejections += 1;
      }
      byStage.set(e.stage, entry);
    }
    // See computeRunningStages' own docstring for why this can't just be `stage.status ===
    // "drafting"`: a non-gated stage's status is stale/misleading between verify attempts.
    const runningStages = computeRunningStages(events);
    for (const [stageKey, entry] of byStage) entry.running = runningStages.has(stageKey);
    return byStage;
  }, [events]);

  const failureIsInfra = failure?.failure_type === "infra_transient" || failure?.failure_type === "quota_exhausted";

  return (
    <ViewContainer>
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-lg font-semibold">Session Overview</h1>
        <LiveCostChip />
      </div>

      {failure && (
        <div
          className={`rounded-lg border p-4 text-sm ${
            failureIsInfra ? "border-amber-300 bg-amber-50 text-amber-900" : "border-red-300 bg-red-50 text-red-900"
          }`}
        >
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="font-semibold">
                Run ended: {failure.stage ?? "unknown stage"} — {failure.type ?? "failure"}
                {failure.failure_type ? ` (${failure.failure_type})` : ""}
              </p>
              {failure.feedback && <p className="mt-1 text-xs">{failure.feedback}</p>}
              <p className="mt-2 text-xs font-medium">
                {failureIsInfra
                  ? "This was an infrastructure/quota failure, not a defect in the work. Resume retries from the last checkpoint."
                  : "A gate rejected the work past its retry budget. Read the feedback above, adjust the requirements if it names a real gap, then Resume — or fix the platform gate first if the feedback looks wrong."}
              </p>
            </div>
            <button
              className="shrink-0 rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              disabled={agent.isRunning}
              // Direct runAgent, NOT ?resume=1: AppShell's autoTriggeredRef never resets and a
              // query-only navigation doesn't remount it, so the URL path would do nothing.
              onClick={() => void copilotkit.runAgent({ agent })}
            >
              {agent.isRunning ? "Resuming…" : "Resume"}
            </button>
          </div>
        </div>
      )}

      {/* Events flow before stage STATE reaches the client (state streams on run pause/gate), so
          "no stages yet" while events are visibly arriving read as broken -- tell the truth: a run
          is underway. */}
      {stages.length === 0 && (
        <p className="text-sm text-neutral-500">
          {events.length > 0
            ? "Run in progress — stages appear here as they start reporting."
            : "No stages have run yet."}
        </p>
      )}

      {stages.length > 0 && (
        <div className="flex flex-col">
          <div className={`${ROW_GRID} px-4 pb-1 text-xs font-medium text-neutral-400`}>
            <span>Stage</span>
            <span className="text-right">Duration</span>
            <span className="text-right">Cost</span>
            <span className="text-right">Redrafts</span>
            <span className="text-right">Status</span>
          </div>
          <ol className="flex flex-col gap-2">
            {stages.map(([key, stage]) => {
              const timing = perStage.get(key);
              const note = stageNote(stage);
              const failedHere = failure?.stage === key;
              // `stage.status` alone misses non-gated stages (ac-to-tests, minimal-code-to-green,
              // remediation, ...): it only updates when the run pauses at a gate, so a stage with
              // no gate can sit at "not_started" for its entire real drafting time. `timing.running`
              // (perStage, above) is derived from the live event stream instead, which has no such
              // lag -- OR'd together since state is never wrong when it does say "drafting".
              const running = stage.status === "drafting" || timing?.running === true;
              return (
                <li
                  key={key}
                  className={`rounded-lg border px-4 py-2 text-sm ${
                    failedHere ? "border-red-300 bg-red-50" : "border-neutral-200"
                  }`}
                >
                  <div className={ROW_GRID}>
                    <span className="font-medium">{key}</span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing && timing.last > timing.first ? formatDuration(timing.last - timing.first) : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing?.sawCost ? `$${timing.cost.toFixed(2)}` : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing && timing.rejections > 0 ? `${timing.rejections}×` : ""}
                    </span>
                    <span
                      className={`flex items-center justify-end gap-1.5 ${failedHere ? "text-red-700" : "text-neutral-500"}`}
                    >
                      {running && <RunningSpinner />}
                      {failedHere ? "Failed" : running ? "Drafting" : (STATUS_LABEL[stage.status] ?? stage.status)}
                    </span>
                  </div>
                  {note && <p className="mt-1 text-xs text-neutral-500">{note}</p>}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </ViewContainer>
  );
}
