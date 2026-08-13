"use client";

import { CopilotSidebar, UseAgentUpdate, useAgent, useCopilotKit, useInterrupt } from "@copilotkit/react-core/v2";
import { useEffect, useRef, useState } from "react";
import { PlanView } from "@/components/PlanView";
import { RequirementsView } from "@/components/RequirementsView";
import { SessionOverview } from "@/components/SessionOverview";
import { SpecificationView } from "@/components/SpecificationView";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { PIPELINE_STAGE_ORDER, type WorkflowState } from "@/lib/workflow-types";

type ViewId = "requirements" | "specification" | "plan" | "overview";

export function AppShell() {
  const { threadId, runtimeAgentId, localAgentId } = useWorkflowThread();
  const { agent } = useAgent({
    agentId: localAgentId,
    runtimeAgentId,
    threadId,
    updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged],
  });
  const [activeView, setActiveView] = useState<ViewId>("requirements");
  const { copilotkit } = useCopilotKit();
  const [sandboxStatus] = useSandboxStatus();

  const state = (agent.state ?? {}) as WorkflowState;
  const specification = state.stages?.specification;
  const plan = state.stages?.plan;

  // Auto-trigger the run once, as soon as the sandbox is ready, on a thread that's never run
  // before -- scaffold_node hard-fails with no local-working-tree fallback if run before the
  // sandbox exists, so this waits on sandboxStatus rather than firing on mount.
  const autoTriggeredRef = useRef(false);
  useEffect(() => {
    if (autoTriggeredRef.current) return;
    if (sandboxStatus !== "ready") return;
    if (Object.keys(state.stages ?? {}).length > 0) return;
    if (agent.messages.length > 0) return;
    autoTriggeredRef.current = true;
    void copilotkit.runAgent({ agent });
  }, [sandboxStatus, state.stages, agent, copilotkit]);

  // Section 8: the approve action for whichever stage is Ready for Review must be reachable
  // regardless of which view is currently open. renderInChat defaults to true (unset here), so
  // this publishes into the CopilotSidebar's chat feed rather than a separate app-level banner
  // -- the sidebar is mounted around every view below, so it stays reachable from any tab.
  //
  // The label is derived from agent.state (each stage's own status), not the interrupt event's
  // own payload -- empirically, resolve() correctly targets whichever gate is actually paused
  // (confirmed: approving resumes the right stage every time), but the interrupt event's payload
  // observed in `render` does not reliably refresh across a second interrupt within the same
  // session, so a label read from it can lag one stage behind. agent.state does update correctly
  // (it drives the tab-enablement logic below, which is correct), so it's the more trustworthy
  // source for display purposes. An ordered lookup, not a binary ternary, since the pipeline now
  // has more than two gated stages -- the first stage in pipeline order sitting at
  // ready_for_review is the one actually paused on the open interrupt.
  useInterrupt<never>({
    agentId: localAgentId,
    render: ({ resolve }) => {
      const readyStage = PIPELINE_STAGE_ORDER.find((s) => state.stages?.[s.key]?.status === "ready_for_review");
      const label = readyStage?.label ?? "this stage";
      return (
        <div className="flex items-center justify-between gap-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
          <span className="text-sm text-amber-900">
            The <strong>{label}</strong> is ready for your review.
          </span>
          <button
            className="rounded-lg bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white"
            onClick={() => resolve({ decision: "approved" })}
          >
            Approve
          </button>
        </div>
      );
    },
  });

  return (
    <div className="flex min-h-full flex-1">
      <div className="flex min-h-full flex-1 flex-col">
        <nav className="flex items-center gap-1 border-b border-neutral-200 px-4 py-2">
          <TabButton
            label="Requirements"
            active={activeView === "requirements"}
            onClick={() => setActiveView("requirements")}
          />
          <TabButton
            label="Specification"
            active={activeView === "specification"}
            disabled={!specification?.ever_ready_for_review}
            onClick={() => setActiveView("specification")}
          />
          <TabButton
            label="Plan"
            active={activeView === "plan"}
            disabled={!plan?.ever_ready_for_review}
            onClick={() => setActiveView("plan")}
          />
          <TabButton
            label="Overview"
            active={activeView === "overview"}
            onClick={() => setActiveView("overview")}
          />
        </nav>

        <main className="flex-1 overflow-y-auto">
          {activeView === "requirements" && <RequirementsView />}
          {activeView === "specification" && <SpecificationView />}
          {activeView === "plan" && (
            <PlanView />
          )}
          {activeView === "overview" && <SessionOverview />}
        </main>
      </div>

      <CopilotSidebar agentId={localAgentId} />
    </div>
  );
}

function TabButton({
  label,
  active,
  disabled,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={[
        "rounded-md px-3 py-1.5 text-sm font-medium",
        active ? "bg-neutral-900 text-white" : "text-neutral-700 hover:bg-neutral-100",
        disabled ? "cursor-not-allowed opacity-40 hover:bg-transparent" : "",
      ].join(" ")}
      disabled={disabled}
      onClick={onClick}
    >
      {label}
    </button>
  );
}
