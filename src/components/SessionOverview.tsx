"use client";

import { useAgent } from "@copilotkit/react-core/v2";
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

/** Structured per-stage timeline -- reads whatever stage keys the backend's current STAGES/
 * standalone specs happen to produce, rather than a hardcoded list, so it never needs updating
 * as pipeline stages are added. ponytail: no live diagram, just the list -- matches the plan's
 * own "structured list in v1, diagram later" scope. */
export function SessionOverview() {
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const stages = Object.entries(state.stages ?? {});

  return (
    <ViewContainer>
      <h1 className="text-lg font-semibold">Session Overview</h1>
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
  );
}
