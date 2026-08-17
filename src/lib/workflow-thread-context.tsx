"use client";

import { createContext, useContext, type ReactNode } from "react";

export const RUNTIME_AGENT_ID = "workflow";

interface WorkflowThreadValue {
  threadId: string;
  runtimeAgentId: string;
  localAgentId: string;
}

const WorkflowThreadContext = createContext<WorkflowThreadValue | null>(null);

/**
 * Supplies the (threadId, runtimeAgentId, localAgentId) triple every useAgent({...}) call in the
 * workflow UI needs to scope itself to one repo/branch/user session (architecture plan Section
 * A) instead of the single shared "workflow" agent every session used to collide on.
 */
export function WorkflowThreadProvider({
  threadId,
  children,
}: {
  threadId: string;
  children: ReactNode;
}) {
  const value: WorkflowThreadValue = {
    threadId,
    runtimeAgentId: RUNTIME_AGENT_ID,
    // useAgent's threadId requires a distinct runtimeAgentId + local agentId pairing, never the
    // shared "workflow" registry id directly -- one call site, inlined rather than its own file.
    localAgentId: `workflow-thread-${threadId}`,
  };
  return <WorkflowThreadContext.Provider value={value}>{children}</WorkflowThreadContext.Provider>;
}

export function useWorkflowThread(): WorkflowThreadValue {
  const value = useContext(WorkflowThreadContext);
  if (!value) {
    throw new Error("useWorkflowThread() called outside a WorkflowThreadProvider");
  }
  return value;
}
