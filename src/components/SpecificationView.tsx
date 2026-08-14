"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { A2UISurfaceView } from "@/components/A2UISurfaceView";
import { ClarifyingQuestions } from "@/components/ClarifyingQuestions";
import { SPECIFICATION_SURFACE_ID } from "@/lib/a2ui-surface-ids";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { WorkflowState } from "@/lib/workflow-types";

export function SpecificationView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-6">
      <ClarifyingQuestions
        stageKey="specification"
        questions={state.stages?.specification?.clarifying_questions ?? []}
        hint="Answer via the chat sidebar — it unlocks whenever the agent is waiting on you."
      />
      <A2UISurfaceView
        surfaceId={SPECIFICATION_SURFACE_ID}
        fallback={<p className="text-sm text-neutral-500">No Specification draft yet.</p>}
      />
    </div>
  );
}
