"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { parseImplementationPlan, PlanSurfaceRenderer } from "@/a2ui/catalog";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { PLAN_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

export function PlanView() {
  // agentId only -- see RequirementsView.tsx's comment: AppShell already registered this
  // proxied agent, re-registering the same id throws.
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const state = (agent.state ?? {}) as WorkflowState;
  const plan = state.stages?.plan;

  // AC-8.4: once reachable, stays visible even after a revision resets it —
  // shown here as a stale banner rather than hiding the last-known content.
  const isStale = plan?.ever_ready_for_review && plan.status === "not_started";

  // Pre-approval fallback: same rationale as SpecificationView -- the surface message only
  // exists post-verify; the streamed draft is the current review target; the interrupt payload
  // is the only source after a reload with the gate open.
  const draft =
    parseImplementationPlan(plan?.draft) ??
    parseImplementationPlan(plan?.approved_content) ??
    (interrupt.stage === "plan" ? parseImplementationPlan(interrupt.draft) : null);

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <ClarifyingQuestions
        stageKey="plan"
        questions={plan?.clarifying_questions ?? []}
        hint="Answer by editing the requirements text on the Requirements tab, then resubmit."
      />
      {isStale && (
        <div className="rounded-lg border border-neutral-300 bg-neutral-50 px-4 py-2 text-sm text-neutral-600">
          The requirements or Specification changed since this Plan was drafted. A new Plan will
          be generated once the Specification is re-approved.
        </div>
      )}
      <A2UISurfaceView
        surfaceId={PLAN_SURFACE_ID}
        fallback={
          draft ? (
            <PlanSurfaceRenderer plan={draft} />
          ) : (
            <p className="text-sm text-neutral-500">No Plan draft yet.</p>
          )
        }
      />
    </div>
  );
}
