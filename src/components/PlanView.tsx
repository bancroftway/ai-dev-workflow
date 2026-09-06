"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { parseImplementationPlan, PlanSurfaceRenderer } from "@/a2ui/catalog";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { AuditFindingsDetails } from "@/components/AuditFindingsDetails";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { Spinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { PLAN_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { useRunActivity } from "@/lib/run-activity-context";
import { deriveStageReviewFlags } from "@/lib/stage-review-flags";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

export function PlanView() {
  // agentId only -- see RequirementsView.tsx's comment: AppShell already registered this
  // proxied agent, re-registering the same id throws.
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const [runActivity] = useRunActivity();
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

  // See stage-review-flags.ts for the isFinal/isProvisional rationale (shared with
  // SpecificationView -- the subtlety here has already caused two live bugs from hand-duplicating
  // this logic).
  const { isProvisional } = deriveStageReviewFlags({
    stageKey: "plan",
    stageStatus: plan?.status,
    interruptOpen: interrupt.open,
    interruptStage: interrupt.stage,
    agentIsRunning: agent.isRunning,
    runActive: runActivity?.runActive,
  });

  // Same shell as Tech Stack / Requirements / Specification (user requirement 2026-08-31):
  // header block on top, content in one bounded 63vh box scrolling internally. Read-only here;
  // approve/reject lives in the gate card above the tabs.
  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Plan</h1>
        <p className="text-sm text-neutral-500">
          Ordered implementation steps, diagrams, and wireframes drafted from the approved
          specification and adversarially audited by a second model. Review here; approve in the
          card above — or revise the requirements document on the Requirements tab and resubmit
          to redraft from it.
        </p>
      </div>

      <AuditFindingsDetails findings={plan?.audit_findings ?? []} />
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
      {isProvisional && (
        <div className="flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs text-amber-900">
          <Spinner className="h-3.5 w-3.5" />
          This is a draft — still being audited. The final version for your review is coming; nothing below is approvable yet.
        </div>
      )}
      <div className="relative h-[75vh]">
        <div
          className={`h-full overflow-y-auto rounded-lg border border-neutral-300 p-3 transition-[filter,opacity] duration-300 ${isProvisional ? "pointer-events-none blur-[0.5px] opacity-90" : ""}`}
        >
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
      </div>
    </ViewContainer>
  );
}
