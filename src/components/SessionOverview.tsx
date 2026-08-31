"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useMemo } from "react";
import { EventLogView } from "@/components/EventLogView";
import { LiveCostChip } from "@/components/LiveCostChip";
import { Swimlane } from "@/components/Swimlane";
import { ViewContainer } from "@/components/ViewContainer";
import { formatDuration, parseEventTs, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { StageState, WorkflowState } from "@/lib/workflow-types";

const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started",
  drafting: "Drafting",
  needs_clarification: "Needs clarification",
  ready_for_review: "Ready for review",
  approved: "Approved",
};

/** The Overview tab -- Part 2 Task 13 wires in the standalone views Tasks 8/9/11 built. Each reads
 * its own data via useRunEvents()/useWorkflowThread() internally, so nothing here plumbs a session
 * id through; this component's only real job is layout. Four sections stacked in one flex column,
 * `divide-y` standing in for a border between them instead of a gap -- NOT one shared
 * `ViewContainer` around all four (every sibling tab uses exactly one, but EventLogView/Swimlane
 * already bring their own internally, by design, since Tasks 8/9 built them to be independently
 * mountable). Each section below still gets exactly one `p-6` inset either way (no double-padding):
 *   1. Title + `LiveCostChip` (live per-run spend, Task 11), side by side.
 *   2. The original per-stage status list, kept as-is -- still the only place gate status
 *      (needs_clarification/ready_for_review/approved) shows up; Swimlane below plots node
 *      *timing*, not gate outcome, so this list is additive, not superseded by it.
 *   3. `Swimlane` (Task 9): wall-clock node/tool timeline. Fixed natural height, no internal
 *      scroll, so `shrink-0` is enough.
 *   4. `EventLogView` (Task 8/12): the detailed log, given the tab's remaining space (`min-h-0
 *      flex-1`) so its own live-follow scroll region (Task 12) has a real bounded height to anchor
 *      against -- the same flex slot it gets when mounted alone as a tab's sole content.
 * ponytail: EventLogView/Swimlane/LiveCostChip each call useRunEvents() independently, so mounting
 * all three here fires 3 duplicate `GET /sessions/{id}/events` fetches. Harmless at today's real
 * event-log sizes; hoist the fetch+subscription into a shared context if that history ever grows
 * large enough for 3x to matter.
 *
 * Why both this tab's LiveCostChip AND MetricsBar's own top-strip costChip stay mounted --
 * investigated for task-13-report.md, not assumed: both sum the exact same underlying
 * `model._last_usage` captures (graph.py writes token_usage to the durable RunEvent this reads AND
 * the sandbox's ephemeral ledger MetricsBar's number derives from, at the same call sites,
 * additively -- run_event_store.py's own docstring), but MetricsBar's number only exists after a
 * commit-triggered background scan finishes or the run's Metrics Exit stage completes, and goes
 * stale the moment the sandbox is torn down (its own refresh helper hard-requires a live sandbox
 * registry entry). LiveCostChip updates continuously from the durable event stream -- live during
 * every pre-commit stage (tech-stack through plan) where MetricsBar's chip is simply absent, and
 * still correct for a finished/historical session with no sandbox left at all. Different
 * cadence/availability, not a duplicate -- kept both. */
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

export function SessionOverview() {
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { copilotkit } = useCopilotKit();
  const state = (agent.state ?? {}) as WorkflowState;
  const stages = Object.entries(state.stages ?? {});
  const failure = state.run_failure;

  // Per-stage wall-clock + spend from the same durable event stream Swimlane/LiveCostChip read:
  // duration is first-to-last event of the stage (includes its tool time and its laps -- "time
  // spent in the stage" as a human means it), cost sums the per-event token_usage.cost the same
  // way LiveCostChip does (null cost counts 0, never poisons the total).
  const events = useRunEvents();
  const perStage = useMemo(() => {
    const byStage = new Map<string, { first: number; last: number; cost: number; sawCost: boolean }>();
    for (const e of events) {
      if (!e.stage) continue;
      const ts = parseEventTs(e.ts);
      const entry = byStage.get(e.stage) ?? { first: ts, last: ts, cost: 0, sawCost: false };
      entry.first = Math.min(entry.first, ts);
      entry.last = Math.max(entry.last, ts);
      const cost = Number((e.token_usage as { cost?: unknown } | null)?.cost);
      if (Number.isFinite(cost)) {
        entry.cost += cost;
        entry.sawCost = true;
      }
      byStage.set(e.stage, entry);
    }
    return byStage;
  }, [events]);

  const failureIsInfra = failure?.failure_type === "infra_transient" || failure?.failure_type === "quota_exhausted";

  return (
    <div className="flex h-full min-h-0 w-full flex-col divide-y divide-neutral-200">
      <div className="shrink-0">
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

          {/* Events flow before stage STATE reaches the client (state streams on run pause/gate),
              so "no stages yet" while the timeline below is visibly busy read as broken -- tell
              the truth: a run is underway. */}
          {stages.length === 0 && (
            <p className="text-sm text-neutral-500">
              {events.length > 0
                ? "Run in progress — stages appear here as they start reporting."
                : "No stages have run yet."}
            </p>
          )}
          <ol className="flex flex-col gap-2">
            {stages.map(([key, stage]) => {
              const timing = perStage.get(key);
              const note = stageNote(stage);
              const failedHere = failure?.stage === key;
              return (
                <li
                  key={key}
                  className={`rounded-lg border px-4 py-2 text-sm ${
                    failedHere ? "border-red-300 bg-red-50" : "border-neutral-200"
                  }`}
                >
                  <div className="flex items-center justify-between gap-4">
                    <span className="font-medium">{key}</span>
                    <span className="flex items-center gap-4">
                      {timing && timing.last > timing.first && (
                        <span className="text-xs text-neutral-500">{formatDuration(timing.last - timing.first)}</span>
                      )}
                      {timing?.sawCost && <span className="text-xs text-neutral-500">${timing.cost.toFixed(2)}</span>}
                      <span className={failedHere ? "text-red-700" : "text-neutral-500"}>
                        {failedHere ? "Failed" : (STATUS_LABEL[stage.status] ?? stage.status)}
                      </span>
                    </span>
                  </div>
                  {note && <p className="mt-1 text-xs text-neutral-500">{note}</p>}
                </li>
              );
            })}
          </ol>
        </ViewContainer>
      </div>

      <div className="shrink-0">
        <Swimlane />
      </div>

      <div className="min-h-0 flex-1">
        <EventLogView />
      </div>
    </div>
  );
}
