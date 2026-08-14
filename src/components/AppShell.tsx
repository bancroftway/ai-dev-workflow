"use client";

import {
  CopilotChatInput,
  type CopilotChatInputProps,
  CopilotSidebar,
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
  useInterrupt,
} from "@copilotkit/react-core/v2";
import { useEffect, useRef, useState } from "react";
import { BuildView } from "@/components/BuildView";
import { MetricsBar, type MetricThresholds } from "@/components/MetricsBar";
import { PlanView } from "@/components/PlanView";
import { QualityView } from "@/components/QualityView";
import { RequirementsView } from "@/components/RequirementsView";
import { SessionOverview } from "@/components/SessionOverview";
import { SpecificationView } from "@/components/SpecificationView";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import {
  type EscalationPayload,
  PIPELINE_STAGE_ORDER,
  TAB_STAGE_GROUPS,
  type StageKey,
  type WorkflowState,
} from "@/lib/workflow-types";

type ViewId = "requirements" | "specification" | "plan" | "build" | "quality" | "overview";
type DotState = "running" | "done" | "error" | "awaiting";

const DOT_CLASS: Record<DotState, string> = {
  running: "bg-blue-500 animate-pulse",
  awaiting: "bg-amber-500 animate-pulse",
  done: "bg-emerald-500",
  error: "bg-red-500",
};

/** Dot for a tab whose status derives from ordinary StageStates (TAB_STAGE_GROUPS). Green dots
 * intentionally clear on resubmission: intake resets later stages to not_started on each fresh
 * run, and the dots simply reflect that. */
function stageGroupDot(state: WorkflowState, keys: StageKey[], error?: boolean): DotState | undefined {
  if (error) return "error";
  const stages = keys.map((k) => state.stages?.[k]).filter((s) => s != null);
  if (stages.length === 0) return undefined;
  if (stages.some((s) => s.status === "drafting")) return "running";
  if (stages.some((s) => s.status === "ready_for_review" || s.status === "needs_clarification")) return "awaiting";
  if (stages.some((s) => s.last_verification && !s.last_verification.passed && s.status !== "approved")) return "error";
  if (stages.every((s) => s.status === "approved")) return "done";
  return undefined;
}

export function AppShell({ metricThresholds }: { metricThresholds: MetricThresholds }) {
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

  // Section 8: the interrupt UI must be reachable regardless of which view is open. renderInChat
  // defaults to true, publishing into the CopilotSidebar's chat feed, which is mounted around
  // every view below.
  //
  // Ordering: the escalation payload's own `type` is checked FIRST -- escalate/security-cap
  // interrupts carry a typed payload (graph.py make_escalate_node, security gate) and rendering
  // them as an approval gate was a real mislabel bug. Only the *label* of a plain approval gate
  // still derives from agent.state rather than the event payload: empirically the payload
  // observed in `render` does not reliably refresh across a second interrupt within the same
  // session (see git history), while agent.state does update correctly. An ordered lookup, not a
  // binary ternary -- the first stage in pipeline order sitting at ready_for_review is the one
  // actually paused on the open interrupt.
  useInterrupt<EscalationPayload>({
    agentId: localAgentId,
    render: ({ resolve, event }) => {
      const payload = (event?.value ?? {}) as EscalationPayload;
      const escalationType = typeof payload === "object" && payload !== null ? payload.type : undefined;

      if (escalationType === "cannot_verify" || escalationType === "verification_cap_exceeded" || escalationType === "security_cycle_cap_exceeded" || escalationType === "exit_gate_failed_twice") {
        const failedStage = PIPELINE_STAGE_ORDER.find(
          (s) => state.stages?.[s.key]?.last_verification && !state.stages[s.key]!.last_verification!.passed,
        );
        const label = (typeof payload.stage === "string" && payload.stage) || failedStage?.label || "this stage";
        const feedback =
          (typeof payload.feedback === "string" && payload.feedback) ||
          failedStage?.key && state.stages?.[failedStage.key]?.last_verification?.feedback ||
          "";
        const heading =
          escalationType === "cannot_verify"
            ? `Cannot verify ${label} — no sandbox is available.`
            : escalationType === "security_cycle_cap_exceeded"
              ? "The security gate is still failing after its retry budget."
              : escalationType === "exit_gate_failed_twice"
                ? "The audit exit gate failed twice."
                : `Verification for ${label} kept failing and needs your attention.`;
        return (
          <div className="space-y-2 rounded-lg border border-red-300 bg-red-50 px-4 py-3">
            <p className="text-sm font-medium text-red-900">{heading}</p>
            {feedback && <p className="text-xs text-red-800">{feedback}</p>}
            <button
              className="rounded-lg bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white"
              onClick={() => resolve({})}
            >
              Acknowledge &amp; retry
            </button>
          </div>
        );
      }

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

  const buildStarted = state.stages?.["ac-to-tests"]?.status !== undefined && state.stages["ac-to-tests"]!.status !== "not_started";
  const qualityStarted = Boolean(
    state.quality_remediation ?? state.security_remediation ?? state.test_hardening ?? state.metrics_report,
  );

  const qualityError =
    state.quality_remediation?.build_ok === false ||
    state.quality_remediation?.last_gate_report?.passed === false ||
    state.security_remediation?.last_gate_report?.passed === false;
  const qualityDone = state.metrics_report?.metrics != null;
  const qualityDot: DotState | undefined = qualityError
    ? "error"
    : qualityDone
      ? "done"
      : qualityStarted && agent.isRunning
        ? "running"
        : undefined;

  const dots: Record<ViewId, DotState | undefined> = {
    requirements: stageGroupDot(state, TAB_STAGE_GROUPS.requirements, state.app_rejection != null),
    specification: stageGroupDot(state, TAB_STAGE_GROUPS.specification),
    plan: stageGroupDot(state, TAB_STAGE_GROUPS.plan),
    build: stageGroupDot(state, TAB_STAGE_GROUPS.build),
    quality: qualityDot,
    overview: undefined,
  };

  return (
    <div className="flex min-h-full flex-1">
      <div className="flex min-h-full flex-1 flex-col">
        {sandboxStatus === "error" && (
          <div className="border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-900">
            Sandbox provisioning failed — the workflow can’t run. Reload the page to retry.
          </div>
        )}
        <MetricsBar thresholds={metricThresholds} />
        <nav className="flex items-center gap-1 border-b border-neutral-200 px-4 py-2">
          <TabButton
            label="Requirements"
            active={activeView === "requirements"}
            dot={dots.requirements}
            onClick={() => setActiveView("requirements")}
          />
          <TabButton
            label="Specification"
            active={activeView === "specification"}
            disabled={!specification?.ever_ready_for_review}
            dot={dots.specification}
            onClick={() => setActiveView("specification")}
          />
          <TabButton
            label="Plan"
            active={activeView === "plan"}
            disabled={!plan?.ever_ready_for_review}
            dot={dots.plan}
            onClick={() => setActiveView("plan")}
          />
          <TabButton
            label="Build"
            active={activeView === "build"}
            disabled={!buildStarted}
            dot={dots.build}
            onClick={() => setActiveView("build")}
          />
          <TabButton
            label="Quality"
            active={activeView === "quality"}
            disabled={!qualityStarted}
            dot={dots.quality}
            onClick={() => setActiveView("quality")}
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
          {activeView === "plan" && <PlanView />}
          {activeView === "build" && <BuildView />}
          {activeView === "quality" && <QualityView />}
          {activeView === "overview" && <SessionOverview />}
        </main>
      </div>

      <CopilotSidebar agentId={localAgentId} input={GatedChatInput} />
    </div>
  );
}

// Chat input gating (WS9): a running agent should not be interrupted by free-text chat, and a
// rejected repo has nothing to chat about. While running, the slot shows a pulsing progress line
// naming the stage currently drafting -- driven by the same AG-UI streamed state AppShell
// subscribes to (this child re-renders with it). Module-level with CopilotChatInput's statics
// copied on (Object.assign) so it satisfies the `typeof CopilotChatInput` slot type and keeps a
// stable identity across renders -- an inline component would remount the input every render and
// drop in-progress text.
const GatedChatInput = Object.assign(
  function GatedChatInputImpl(props: CopilotChatInputProps) {
    const { localAgentId } = useWorkflowThread();
    const { agent } = useAgent({ agentId: localAgentId });
    const state = (agent.state ?? {}) as WorkflowState;
    const rejected = state.app_rejection != null;
    if (agent.isRunning || rejected) {
      const draftingStage = PIPELINE_STAGE_ORDER.find((s) => state.stages?.[s.key]?.status === "drafting");
      return (
        <div className="border-t border-neutral-200 px-4 py-3 text-sm text-neutral-400">
          {rejected ? (
            "This repository was rejected — chat is closed."
          ) : (
            <span className="animate-pulse">
              ⋯ {draftingStage ? `Drafting ${draftingStage.label}` : "Working"} — chat opens when the agent needs you
            </span>
          )}
        </div>
      );
    }
    return <CopilotChatInput {...props} />;
  },
  CopilotChatInput,
);

function TabButton({
  label,
  active,
  disabled,
  dot,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  dot?: DotState;
  onClick: () => void;
}) {
  return (
    <button
      className={[
        "flex items-center rounded-md px-3 py-1.5 text-sm font-medium",
        active ? "bg-neutral-900 text-white" : "text-neutral-700 hover:bg-neutral-100",
        disabled ? "cursor-not-allowed opacity-40 hover:bg-transparent" : "",
      ].join(" ")}
      disabled={disabled}
      onClick={onClick}
    >
      {label}
      {dot && <span aria-hidden className={`ml-1.5 inline-block h-2 w-2 rounded-full ${DOT_CLASS[dot]}`} />}
    </button>
  );
}
