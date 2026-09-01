"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useMemo } from "react";
import { RunningSpinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { computeRunningStages, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { StageState, WorkflowState } from "@/lib/workflow-types";

// Both stages this view renders (BUILD_STAGES below) are non-gated -- no human ever reviews them
// (README: only tech-stack/specification/plan pause for a person). "Ready for review" is the
// backend's generic status name for "draft done, deterministic verify next", shared with the
// gated stages where it genuinely does mean a human's turn -- reusing that wording here read as
// "ready for whose review?" with no gate in sight (user feedback 2026-09-01). This map is local to
// this file, so the wording fix can't affect Specification/Plan's own (correct) usage of it.
const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started",
  drafting: "Drafting",
  needs_clarification: "Needs clarification",
  ready_for_review: "Auto-verifying",
  approved: "Approved",
};

const BUILD_STAGES: { key: "ac-to-tests" | "minimal-code-to-green"; label: string; blurb: string }[] = [
  { key: "ac-to-tests", label: "Acceptance Criteria to Tests", blurb: "Failing tests written from the approved acceptance criteria." },
  { key: "minimal-code-to-green", label: "Minimal Code to Green", blurb: "The smallest implementation that makes those tests pass." },
];

function StageCard({
  stageKey,
  label,
  blurb,
  stage,
  runFailure,
  running,
}: {
  stageKey: string;
  label: string;
  blurb: string;
  stage?: StageState;
  runFailure?: WorkflowState["run_failure"];
  // Live from the event stream (computeRunningStages), not `stage.status` alone: this view's two
  // stages are both non-gated, so `status` sits stuck at whatever it was before the current draft
  // (often "not_started", or a stale "ready_for_review" from the last verify attempt) for the
  // entire time a turn is actually running server-side -- confirmed live 2026-09-01,
  // minimal-code-to-green's .out file was actively growing while this card still said "Not
  // started". Same fix as AppShell's tab pills and SessionOverview's table.
  running: boolean;
}) {
  const verification = stage?.last_verification;
  // Same guard as AppShell's tab dot: an approved stage's stale failed verification is history,
  // not something the user should still be staring at in red.
  const showFailure = Boolean(verification && !verification.passed && stage?.status !== "approved");
  const failedHere = runFailure?.stage === stageKey;
  const cap = stage?.max_verify_cycles || undefined;
  return (
    <section className="space-y-2 rounded-lg border border-neutral-200 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{label}</h2>
        <span className="flex items-center gap-1.5 text-sm text-neutral-500">
          {running && <RunningSpinner />}
          {running ? "Drafting" : (STATUS_LABEL[stage?.status ?? "not_started"] ?? stage?.status)}
        </span>
      </div>
      <p className="text-xs text-neutral-500">{blurb}</p>
      {stage && (
        <div className="flex gap-4 text-xs text-neutral-600">
          <span>clarification cycles: {stage.cycle_count}</span>
          <span>verify retries: {stage.verify_cycle_count ?? 0}{cap ? ` of ${cap}` : ""}</span>
          {(stage.infra_retry_count ?? 0) > 0 && <span>infra retries: {stage.infra_retry_count}</span>}
          <span>audit findings: {stage.audit_findings?.length ?? 0}</span>
        </div>
      )}
      {showFailure && verification && (
        <div className="rounded-md border border-red-300 bg-red-50 p-2 text-xs text-red-900">
          <span className="font-medium">Last verification failed{verification.cannot_verify ? " (no sandbox)" : ""}:</span>{" "}
          {verification.feedback}
          <p className="mt-1 font-medium">
            {failedHere
              ? "This run ended here. See the Overview tab for details and Resume."
              : "The pipeline retries this automatically — no action is needed unless the run ends in failure."}
          </p>
        </div>
      )}
    </section>
  );
}

export function BuildView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const runEvents = useRunEvents();
  const runningStages = useMemo(() => computeRunningStages(runEvents), [runEvents]);

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Build</h1>
        <p className="text-sm text-neutral-500">Tests-first implementation progress after the approved plan.</p>
      </div>
      {BUILD_STAGES.map(({ key, label, blurb }) => (
        <StageCard
          key={key}
          stageKey={key}
          label={label}
          blurb={blurb}
          stage={state.stages?.[key]}
          runFailure={state.run_failure}
          running={runningStages.has(key)}
        />
      ))}
    </ViewContainer>
  );
}
