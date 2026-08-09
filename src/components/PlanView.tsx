"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { PLAN_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import type { WorkflowState } from "@/lib/workflow-types";

export function PlanView() {
  const { agent } = useAgent({ agentId: "workflow" });
  const state = (agent.state ?? {}) as WorkflowState;
  const plan = state.stages?.plan;

  // AC-8.4: once reachable, stays visible even after a revision resets it —
  // shown here as a stale banner rather than hiding the last-known content.
  const isStale = plan?.ever_ready_for_review && plan.status === "not_started";

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      {isStale && (
        <div className="rounded-lg border border-neutral-300 bg-neutral-50 px-4 py-2 text-sm text-neutral-600">
          The requirements or Specification changed since this Plan was drafted. A new Plan will
          be generated once the Specification is re-approved.
        </div>
      )}
      <A2UISurfaceView
        surfaceId={PLAN_SURFACE_ID}
        fallback={<p className="text-sm text-neutral-500">No Plan draft yet.</p>}
      />
    </div>
  );
}
