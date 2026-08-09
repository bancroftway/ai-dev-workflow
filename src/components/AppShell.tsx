"use client";

import { UseAgentUpdate, useAgent, useInterrupt } from "@copilotkit/react-core/v2";
import { useState } from "react";
import { PlanView } from "@/components/PlanView";
import { RequirementsView } from "@/components/RequirementsView";
import { SpecificationView } from "@/components/SpecificationView";
import type { GatePayload, WorkflowState } from "@/lib/workflow-types";

type ViewId = "requirements" | "specification" | "plan";

export function AppShell() {
  const { agent } = useAgent({
    agentId: "workflow",
    updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged],
  });
  const [activeView, setActiveView] = useState<ViewId>("requirements");

  const state = (agent.state ?? {}) as WorkflowState;
  const specification = state.stages?.specification;
  const plan = state.stages?.plan;

  // Section 8: the approve action for whichever stage is Ready for Review
  // must be reachable regardless of which view is currently open.
  const approveElement = useInterrupt<never, false>({
    agentId: "workflow",
    renderInChat: false,
    render: ({ event, resolve }) => {
      const payload = event.value as GatePayload | undefined;
      const label = payload?.stage === "plan" ? "Implementation Plan" : "Specification";
      return (
        <div className="flex items-center justify-between gap-4 border-b border-amber-300 bg-amber-50 px-6 py-3">
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
    <div className="flex min-h-full flex-col">
      {approveElement}

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
        {agent.isRunning && (
          <span className="ml-auto text-xs text-neutral-500">Generating…</span>
        )}
      </nav>

      <main className="flex-1 overflow-y-auto">
        {activeView === "requirements" && <RequirementsView />}
        {activeView === "specification" && <SpecificationView />}
        {activeView === "plan" && (
          <PlanView />
        )}
      </main>
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
