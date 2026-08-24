"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { EventLogView } from "@/components/EventLogView";
import { LiveCostChip } from "@/components/LiveCostChip";
import { Swimlane } from "@/components/Swimlane";
import { ViewContainer } from "@/components/ViewContainer";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

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
export function SessionOverview() {
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const stages = Object.entries(state.stages ?? {});

  return (
    <div className="flex h-full min-h-0 w-full flex-col divide-y divide-neutral-200">
      <div className="shrink-0">
        <ViewContainer>
          <div className="flex items-start justify-between gap-4">
            <h1 className="text-lg font-semibold">Session Overview</h1>
            <LiveCostChip />
          </div>
          {stages.length === 0 && <p className="text-sm text-neutral-500">No stages have run yet.</p>}
          <ol className="flex flex-col gap-2">
            {stages.map(([key, stage]) => (
              <li key={key} className="flex items-center justify-between rounded-lg border border-neutral-200 px-4 py-2 text-sm">
                <span className="font-medium">{key}</span>
                <span className="text-neutral-500">{STATUS_LABEL[stage.status] ?? stage.status}</span>
              </li>
            ))}
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
