"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { parseSpecification, SpecificationSurfaceRenderer } from "@/a2ui/catalog";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { ViewContainer } from "@/components/ViewContainer";
import { SPECIFICATION_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

export function SpecificationView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { interrupt } = useOpenInterrupt();
  const state = (agent.state ?? {}) as WorkflowState;
  const stage = state.stages?.specification;

  // Pre-approval fallback: the A2UI surface message only exists after deterministic verify, and
  // vanishes on reload -- render the streamed draft (current review target; approved_content is
  // the PREVIOUS run's content) so the gate is never a blind approve. Last resort: the draft
  // carried inside a re-emitted gate interrupt (the only data available after a reload while the
  // gate is open).
  const draft =
    parseSpecification(stage?.draft) ??
    parseSpecification(stage?.approved_content) ??
    (interrupt.stage === "specification" ? parseSpecification(interrupt.draft) : null);

  return (
    <ViewContainer>
      <ClarifyingQuestions
        stageKey="specification"
        questions={stage?.clarifying_questions ?? []}
        hint="Answer by editing the requirements text on the Requirements tab, then resubmit."
      />
      <A2UISurfaceView
        surfaceId={SPECIFICATION_SURFACE_ID}
        fallback={
          draft ? (
            <SpecificationSurfaceRenderer specification={draft} />
          ) : (
            <p className="text-sm text-neutral-500">No Specification draft yet.</p>
          )
        }
      />
    </ViewContainer>
  );
}
