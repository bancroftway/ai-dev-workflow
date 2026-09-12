"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { Fragment, useMemo } from "react";
import { RunningSpinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { useRunActivity } from "@/lib/run-activity-context";
import { computeRunningPhases, NODE_PHASE_LABEL, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import {
  REBUILD_PLACEMENTS,
  REBUILD_STATUS_LABEL,
  rebuildPhase,
  stageOrderIndex,
  type StageState,
  type WorkflowState,
} from "@/lib/workflow-types";

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
  runningLabel,
  knownComplete,
  runActive,
}: {
  stageKey: string;
  label: string;
  blurb: string;
  stage?: StageState;
  runFailure?: WorkflowState["run_failure"];
  // Live from the event stream (computeRunningPhases), not `stage.status` alone: this view's two
  // stages are both non-gated, so `status` sits stuck at whatever it was before the current draft
  // (often "not_started", or a stale "ready_for_review" from the last verify attempt) for the
  // entire time a turn is actually running server-side -- confirmed live 2026-09-01,
  // minimal-code-to-green's .out file was actively growing while this card still said "Not
  // started". Same fix as AppShell's tab pills and SessionOverview's table. The resolved phase
  // label (e.g. "Auditing"), or null when this stage isn't currently running.
  runningLabel: string | null;
  // Mid-run reattach gap (fold-in fix, 2026-09-11): `stage` is undefined and `runningLabel` is null
  // for BOTH Build stages while state.stages hasn't hydrated yet -- without this, a stage the
  // durable current_stage already confirms finished (e.g. current_stage is "remediation") still
  // said "Not started" until the next snapshot landed. See BuildView()'s own comment for why
  // current_stage (approval-only) is the right signal for THIS specific question.
  knownComplete?: boolean;
  // Root-caused 2026-09-12 (user-reported: "why does this say waiting to sync forever?"): the
  // "waiting to sync" wording assumed the gap was always transient -- a live run actively
  // progressing, a fresh snapshot arriving within seconds. Post-pivot, nothing auto-fires a live
  // run anymore, so an idle session sits at knownComplete with no run active INDEFINITELY -- "waiting
  // to sync" is simply false in that case, nothing is syncing. Only show it while a run is
  // genuinely active; otherwise the durable fact alone is the complete, final answer.
  runActive?: boolean;
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
          {runningLabel && <RunningSpinner />}
          {runningLabel ??
            (stage
              ? (STATUS_LABEL[stage.status] ?? stage.status)
              : knownComplete
                ? runActive
                ? "Completed — waiting for full detail to sync…"
                : "Completed"
              : STATUS_LABEL["not_started"])}
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

/** User-reported gap (2026-09-06): several minutes of real work (a clean-build check,
 * agent/src/rebuild.py's rebuild_node -- plus a TDD-red check, ac-to-tests' placement only) happen
 * between a Build stage approving and the next one showing any activity, with nothing on-screen in
 * between -- read as stalled. See rebuildPhase's own docstring (workflow-types.ts) for the
 * derivation. A slim connector row, not a full StageCard: this step is infrastructure the pipeline
 * always runs, never something a human reviews the way the two real Build stages are. `isRedGate`
 * picks the one placement (ac-to-tests') with a TDD-red check worth naming specifically; the other
 * (after minimal-code-to-green, only reachable here if that stage's OWN rebuild is still running
 * when a user checks this tab before moving to Quality) gets the same generic copy
 * REBUILD_STATUS_LABEL already gives it elsewhere. */
export function RebuildConnector({
  running,
  status,
  failedHere,
  isRedGate,
}: {
  running: boolean;
  status: "not_started" | "clean" | "failed" | "fixing";
  failedHere: boolean;
  isRedGate: boolean;
}) {
  const label = failedHere
    ? `${isRedGate ? "Red-gate" : "Rebuild"} check failed — see the Overview tab for details and Resume.`
    : running
      ? isRedGate
        ? "Confirming the new tests actually fail before implementation starts…"
        : "Running a clean-build check before the next stage starts…"
      : status === "clean" && isRedGate
        ? "Red-gate check passed — tests confirmed red before implementation began."
        : REBUILD_STATUS_LABEL[status];
  return (
    <div className={`flex items-center gap-2 px-1 text-xs ${failedHere ? "text-red-700" : "text-neutral-500"}`}>
      {running && <RunningSpinner />}
      <span>{label}</span>
    </div>
  );
}

export function BuildView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const runEvents = useRunEvents();
  const [runActivity] = useRunActivity();
  const runningPhases = useMemo(
    () => computeRunningPhases(runEvents, runActivity?.runActive ?? null),
    [runEvents, runActivity?.runActive],
  );
  // Mid-run reattach gap (fold-in fix, 2026-09-11): current_stage advances both on a stage's OWN
  // approval AND right before that stage's own draft starts (graph.py's make_draft_node) -- either
  // way, current_stage moving PAST key (strictly greater index) can only happen once key's own
  // gate/verify has passed (the pipeline is sequentially gated), so it remains a reliable "key is
  // done" signal regardless of which of the two write sites produced it. Used only as a fallback
  // below, when neither `stage` nor `runningLabel` has data yet.
  const currentStageIdx = stageOrderIndex(runActivity?.currentStage);
  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Build</h1>
        <p className="text-sm text-neutral-500">Tests-first implementation progress after the approved plan.</p>
      </div>
      {BUILD_STAGES.map(({ key, label, blurb }) => {
        const placement = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === key);
        const phase = placement && rebuildPhase(state, placement, runActivity?.runActive, runningPhases);
        const stage = state.stages?.[key];
        // Pivot (root-caused 2026-09-12): `!runActivity?.interrupted` used to gate this off too,
        // out of excess caution -- but current_stage having moved past `key` is a durable fact
        // regardless of whether anything is currently attached to the run; interrupted-ness says
        // nothing about whether that historical fact is trustworthy, and excluding it was exactly
        // what left this tab showing "Not started" on an interrupted-but-long-finished session.
        const knownComplete =
          stage == null &&
          currentStageIdx >= 0 &&
          currentStageIdx > stageOrderIndex(key);
        return (
          <Fragment key={key}>
            <StageCard
              stageKey={key}
              label={label}
              blurb={blurb}
              stage={stage}
              runFailure={state.run_failure}
              runningLabel={runningPhases.has(key) ? (NODE_PHASE_LABEL[runningPhases.get(key)!] ?? "Running") : null}
              knownComplete={knownComplete}
              runActive={runActivity?.runActive}
            />
            {placement && phase && (
              <RebuildConnector
                running={phase.running}
                status={phase.status}
                failedHere={state.run_failure?.stage === placement.rebuildKey}
                isRedGate={placement.rebuildKey === "r_ac_to_tests"}
              />
            )}
          </Fragment>
        );
      })}
    </ViewContainer>
  );
}
