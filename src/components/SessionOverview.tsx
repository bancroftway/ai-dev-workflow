"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { Fragment, useMemo, useState } from "react";
import { RunningSpinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { useRunActivity } from "@/lib/run-activity-context";
import { computeRunningPhases, NODE_PHASE_LABEL, formatDuration, parseEventTs, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import {
  PIPELINE_STAGE_ORDER,
  REBUILD_PLACEMENTS,
  REBUILD_STATUS_LABEL,
  realStageForFailure,
  rebuildPhase,
  stageOrderIndex,
  type RebuildPlacement,
  type StageState,
  type WorkflowState,
} from "@/lib/workflow-types";

const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started",
  drafting: "Drafting",
  needs_clarification: "Needs clarification",
  ready_for_review: "Ready for review",
  approved: "Approved",
};

/** The Overview tab: session cost + a per-stage table (duration, spend, redraft count, status).
 * Timeline (Swimlane.tsx) and the detailed event log (EventLogView.tsx) were removed here (user
 * request 2026-09-01) along with their now-dead component files (and DiffView.tsx, which existed
 * only to render EventLogView's diff payloads) -- this was the only place either was mounted. */

/** One stage's short human-facing note: the live failure feedback while it's failing, else what
 * the audit did, else the approved summary. Never the raw draft. */
function stageNote(stage: StageState): string | null {
  const v = stage.last_verification;
  if (v && !v.passed && stage.status !== "approved") return truncate(v.feedback, 140);
  if (stage.audit_findings?.length) return `${stage.audit_findings.length} audit finding(s) addressed`;
  const summary = (stage.approved_content as { summary?: string } | null)?.summary;
  if (stage.status === "approved" && summary) return truncate(summary, 140);
  return null;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// Shared between the header row and every stage row so the columns actually line up like a table
// (user feedback 2026-09-01) instead of each row's flex layout drifting with its own content width.
const ROW_GRID = "grid grid-cols-[1fr_4.5rem_4rem_5rem_9rem] items-center gap-3";

/** One REBUILD_PLACEMENTS row, inserted right after its `afterStageKey`'s own row (rebuildPhase's
 * own docstring: real, unattributed-to-a-single-placement work happening between two stages).
 * `timing` is windowed between the two REAL stages either side of this placement (rebuildTimings,
 * below) rather than read off the ambiguous shared "rebuild" event tag directly -- that sidesteps
 * the tag collision across placements (workflow-types.ts's REBUILD_PLACEMENTS docstring) since the
 * window itself is placement-specific even when the tag inside it isn't. Only set once the next
 * real stage has started (window closed); still-running placements show status only, same as
 * before. */
function RebuildRow({
  placement,
  phase,
  timing,
  failedHere,
}: {
  placement: RebuildPlacement;
  phase: { status: "not_started" | "clean" | "failed" | "fixing"; running: boolean };
  timing: { first: number; last: number; cost: number } | undefined;
  failedHere: boolean;
}) {
  return (
    <li className={`rounded-lg border px-4 py-2 text-sm ${failedHere ? "border-red-300 bg-red-50" : "border-neutral-200"}`}>
      <div className={ROW_GRID}>
        <span className="font-medium">{placement.label}</span>
        <span className="text-right text-xs text-neutral-500">
          {timing ? formatDuration(timing.last - timing.first) : ""}
        </span>
        <span className="text-right text-xs text-neutral-500">{timing ? `$${timing.cost.toFixed(2)}` : ""}</span>
        <span className="text-right text-xs text-neutral-500" />
        <span className={`flex items-center justify-end gap-1.5 ${failedHere ? "text-red-700" : "text-neutral-500"}`}>
          {phase.running && <RunningSpinner />}
          {failedHere ? "Failed" : phase.running ? "Verifying" : REBUILD_STATUS_LABEL[phase.status]}
        </span>
      </div>
    </li>
  );
}


export function SessionOverview() {
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const { copilotkit } = useCopilotKit();
  const state = (agent.state ?? {}) as WorkflowState;
  // Root-caused 2026-09-12 (user-reported: stages rendered out of pipeline order): `state.stages`
  // is a plain object -- Object.entries has no ordering guarantee of its own, only whatever order
  // the backend happened to insert keys in. Sort by the same PIPELINE_STAGE_ORDER every other
  // ordering decision on this page already uses.
  const stages = Object.entries(state.stages ?? {}).sort(([a], [b]) => stageOrderIndex(a) - stageOrderIndex(b));
  const failure = state.run_failure;
  const [runActivity] = useRunActivity();

  // Pivot (root-caused 2026-09-12): "last successful stage" / "the stage that failed" must be
  // visible durably, without needing a live snapshot -- and exactly one action (this section)
  // may ever advance the graph anywhere in the app. `failure_stage` is often NOT one of the 8 real
  // stage keys (a rebuild placement, e2e, test-hardening, or metrics-regression escalate all name
  // their own key) -- realStageForFailure resolves the real stage a restart should target.
  //
  // Root-caused 2026-09-12 (user-reported: "Continue" shown on an already-Approved remediation
  // row): `runActivity` (dbo.sessions, via SSE) can legitimately lag behind genuine further
  // progress -- current_stage stuck at an earlier stage even though the checkpoint/live state
  // shows the run continued past it and approved everything since. Now that the checkpoint
  // hydration fix (above, AppShell.tsx) makes live `state.stages` reliably available even for an
  // idle session, prefer LIVE signals for the boundary the instant there's live data to read:
  // `state.run_failure` (this exact run's own live escalation payload) over the durable
  // `runActivity.failureStage`, and (see firstNonApprovedKey below) live per-stage status over the
  // durable `runActivity.currentStage` for the non-failed case. Falls back to the durable-only
  // signals exactly when there's truly no live data at all (stages.length === 0, the empty-tabs
  // branch below) -- unchanged from before.
  const hasLiveStages = stages.length > 0;
  // Root-caused 2026-09-12 (user-reported: boundary landed on Preflight Baseline, a stage this run
  // never needed): scanning forward for the FIRST non-approved stage picks up any earlier stage
  // that's conditionally skipped for this run type (brownfield-baseline stays "not_started"
  // forever on a run that never needed it) -- that's not a frontier, it's a stage the pipeline
  // deliberately never touches. Scan for the LATEST stage with any non-"not_started" status
  // instead: if that stage is approved, the frontier is whatever's next after it (or nothing, if
  // it was the last real stage -- a fully successful run); otherwise that stage itself -- still
  // incomplete -- IS the frontier.
  const orderedRealStages = PIPELINE_STAGE_ORDER.filter((s) => state.stages?.[s.key] != null);
  let lastTouchedIdx = -1;
  orderedRealStages.forEach((s, i) => {
    if ((state.stages![s.key]!.status ?? "not_started") !== "not_started") lastTouchedIdx = i;
  });
  const firstNonApprovedKey = !hasLiveStages
    ? null
    : lastTouchedIdx === -1
      ? (orderedRealStages[0]?.key ?? null)
      : state.stages![orderedRealStages[lastTouchedIdx].key]!.status === "approved"
        ? (orderedRealStages[lastTouchedIdx + 1]?.key ?? null)
        : orderedRealStages[lastTouchedIdx].key;
  const mappedFailureTarget = hasLiveStages
    ? failure
      ? realStageForFailure(failure.stage ?? null)
      : null
    : runActivity?.status === "failed" && !runActivity?.finishedWithVerdict
      ? realStageForFailure(runActivity?.failureStage ?? null)
      : null;
  // Boundary = where "last known-good" ends. A genuine failure's mapped target is the TRUE
  // boundary: current_stage can be pushed all the way to metrics-exit by the crash-reporting pass
  // that still runs after most escalates, which would otherwise misreport "everything through
  // Metrics & Exit succeeded" (confirmed against graph.py: metrics-exit_draft's own draft-start
  // write bumps current_stage regardless of why it was reached). When nothing failed (or the
  // session finished with a real verdict), current_stage is fully trustworthy as-is.
  const boundaryKey = mappedFailureTarget ?? (hasLiveStages ? firstNonApprovedKey : (runActivity?.currentStage ?? null));
  const boundaryIdx = stageOrderIndex(boundaryKey);
  const isFailedBoundary = mappedFailureTarget != null;
  // Same live-over-durable preference as the boundary itself, for the actual error text shown
  // next to the restart button -- the durable copies are only a truncated mirror of this same data.
  const failureTypeText = hasLiveStages ? (failure?.type ?? failure?.failure_type ?? null) : (runActivity?.failureType ?? null);
  const failureMessageText = hasLiveStages ? (failure?.feedback ?? null) : (runActivity?.failureMessage ?? null);
  // finished_with_verdict (metrics-exit itself approved a real report, whether or not merge_ready
  // came back true) never gets a restart button anywhere -- redoing metrics-exit alone would just
  // reproduce the same verdict against unchanged upstream work; the Report tab already has it.
  const finishedWithVerdict = runActivity?.finishedWithVerdict ?? false;

  const [restarting, setRestarting] = useState(false);
  // The one and only place the graph is ever advanced by this frontend (pivot requirement).
  // Two shapes, not one: a genuine failure needs the full soft-rewind (reset that stage + every
  // stage after it, confirmed destructive-ish) -- but "nothing failed, just continue from where
  // it left off" needs no reset at all (the frontier stage was never approved to begin with), so
  // it's just a plain reattach, with no server-side rewind call and no `status=="failed"` 409 risk
  // (sessions_api.py's rewind-to-stage action requires that status, which a merely-interrupted
  // in_progress session never has).
  async function handleRestart(stageKey: string) {
    const label = PIPELINE_STAGE_ORDER.find((s) => s.key === stageKey)?.label ?? stageKey;
    if (isFailedBoundary) {
      if (
        !window.confirm(
          `This resets ${label} and everything after it, then redoes that work from scratch. ` +
            "Files the failed attempt already wrote are not reverted -- the redraft may leave some behind. Continue?",
        )
      ) {
        return;
      }
      setRestarting(true);
      try {
        const response = await fetch("/api/sessions/actions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sessionId: threadId, action: "rewind-to-stage", stageKey }),
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          window.alert(body?.detail || "Could not restart this session.");
          return;
        }
        void copilotkit.runAgent({ agent });
      } finally {
        setRestarting(false);
      }
      return;
    }
    if (!window.confirm(`Continue this workflow at ${label}?`)) return;
    setRestarting(true);
    try {
      void copilotkit.runAgent({ agent });
    } finally {
      setRestarting(false);
    }
  }

  // Cheap re-verify (root-caused 2026-09-12, user-reported: a run that finishes merge_ready=false
  // offers NO in-app action at all -- `handleRestart`'s isFailedBoundary branch can't be reused
  // here: `realStageForFailure` deliberately returns null for failure_stage=="exit" (metrics-exit
  // approving with merge_ready=false is a real, verdict-bearing finish, not the "escalate" shape
  // that function maps), so `isFailedBoundary` is false and handleRestart would silently take the
  // no-op "just continue" path instead of actually calling rewind-to-stage. This is a separate,
  // explicit action: reset ONLY metrics-exit (sessions_api.py's rewind-to-stage already allows this
  // -- status=="failed" and current_stage=="metrics-exit" both already satisfied here -- no earlier
  // stage's work is touched or re-billed).
  async function handleReverifyMetricsExit() {
    if (
      !window.confirm(
        "Re-run only Metrics & Exit against the current code -- this does not reset or redo any " +
          "earlier stage. Use this after you've fixed the blocking reasons yourself (or via a " +
          "targeted fix). Continue?",
      )
    ) {
      return;
    }
    setRestarting(true);
    try {
      const response = await fetch("/api/sessions/actions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: threadId, action: "rewind-to-stage", stageKey: "metrics-exit" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        window.alert(body?.detail || "Could not re-verify this session.");
        return;
      }
      void copilotkit.runAgent({ agent });
    } finally {
      setRestarting(false);
    }
  }

  // Seeded targeted-fix (root-caused 2026-09-12, "a way to remedy without starting over and
  // wasting tokens"): asks the agent itself to fix the prior run's own blocking_reasons directly
  // against the current code, then automatically re-verifies at Metrics & Exit -- no manual
  // patching required, and no full stage redo. Purely additive server-side (sessions_api.py's
  // targeted-fix action never resets any stage but metrics-exit, and only after the fix runs).
  async function handleTargetedFix() {
    if (
      !window.confirm(
        "Ask the agent to fix the blocking reasons from this run's own report, directly against " +
          "the current code, then automatically re-verify Metrics & Exit. This does not redo any " +
          "earlier stage. Continue?",
      )
    ) {
      return;
    }
    setRestarting(true);
    try {
      const response = await fetch("/api/sessions/actions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: threadId, action: "targeted-fix" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        window.alert(body?.detail || "Could not start a targeted fix for this session.");
        return;
      }
      void copilotkit.runAgent({ agent });
    } finally {
      setRestarting(false);
    }
  }

  // Per-stage wall-clock + spend from the durable event stream, plus a redraft/rejection count:
  // a gate_resolved event with payload.decision === "rejected" is exactly a human rejection that
  // sent the stage back to its own draft node (make_gate_node, graph.py) -- a count Overview never
  // surfaced before (user feedback 2026-09-01).
  const events = useRunEvents();
  // Lifted out of perStage's own memo below (which used to compute this only for its own local
  // use) so RebuildRow's phase check (further down) can share the exact same fast-channel signal
  // instead of falling back to the slower state-snapshot check alone -- see rebuildPhase's own
  // docstring, "two stages active at once" (root-caused 2026-09-11).
  const runningPhases = useMemo(
    () => computeRunningPhases(events, runActivity?.runActive ?? null),
    [events, runActivity?.runActive],
  );
  const perStage = useMemo(() => {
    const byStage = new Map<
      string,
      { first: number; last: number; cost: number; sawCost: boolean; rejections: number; node: string | undefined }
    >();
    for (const e of events) {
      if (!e.stage) continue;
      const ts = parseEventTs(e.ts);
      const entry =
        byStage.get(e.stage) ?? { first: ts, last: ts, cost: 0, sawCost: false, rejections: 0, node: undefined };
      entry.first = Math.min(entry.first, ts);
      entry.last = Math.max(entry.last, ts);
      const cost = Number((e.token_usage as { cost?: unknown } | null)?.cost);
      if (Number.isFinite(cost)) {
        entry.cost += cost;
        entry.sawCost = true;
      }
      if (e.type === "gate_resolved" && (e.payload as { decision?: string } | null)?.decision === "rejected") {
        entry.rejections += 1;
      }
      byStage.set(e.stage, entry);
    }
    // See computeRunningPhases' own docstring for why this can't just be `stage.status ===
    // "drafting"`: a non-gated stage's status is stale/misleading between verify attempts.
    for (const [stageKey, entry] of byStage) entry.node = runningPhases.get(stageKey);
    return byStage;
  }, [events, runningPhases]);

  // Per-placement duration/cost, windowed between the two real stages either side (see RebuildRow's
  // docstring) rather than trusting the shared "rebuild"/"red-gate" event tag alone. Requires the
  // next real stage to have started (window closed) -- an in-flight placement has no `next` entry
  // yet and is left out, same as before this existed.
  const rebuildTimings = useMemo(() => {
    const timings = new Map<string, { first: number; last: number; cost: number }>();
    for (const placement of REBUILD_PLACEMENTS) {
      const after = perStage.get(placement.afterStageKey);
      const next = perStage.get(placement.nextStageKey);
      if (!after || !next) continue;
      const start = after.last;
      const end = next.first;
      let cost = 0;
      for (const e of events) {
        if (e.stage !== "rebuild" && e.stage !== "red-gate") continue;
        const ts = parseEventTs(e.ts);
        if (ts < start || ts > end) continue;
        const c = Number((e.token_usage as { cost?: unknown } | null)?.cost);
        if (Number.isFinite(c)) cost += c;
      }
      timings.set(placement.rebuildKey, { first: start, last: end, cost });
    }
    return timings;
  }, [events, perStage]);

  return (
    <ViewContainer>
      {/* No local cost chip here: MetricsBar (AppShell) already mounts its own LiveCostChip as
          soon as there's any live cost to show -- a second one here was a plain duplicate, not a
          fallback for an actually-uncovered case (root-caused 2026-09-11, user-reported dupe). */}
      <h1 className="text-lg font-semibold">Session Overview</h1>

      {/* Pivot (root-caused 2026-09-12): the old top banner here read live `state.run_failure`
          (empty for the whole reattach gap) and fired a bare, untargeted `runAgent()` -- no confirm,
          no stage targeting, no rebuild-sub-state reset, bypassing the "exactly one explicit,
          stage-anchored action" requirement entirely. Removed: `runActivity.failureType`/
          `failureMessage` (durable, truncated from the SAME feedback text this used to show live)
          now render on the boundary row itself, below, alongside the one real restart action. */}

      {/* Events flow before stage STATE reaches the client (state streams on run pause/gate), so
          "no stages yet" while events are visibly arriving read as broken -- tell the truth: a run
          is underway. Mid-run reattach gap (same fold-in fix as BuildView's knownComplete,
          2026-09-11): state.stages is empty for the WHOLE reattach gap, which could otherwise leave
          this tab stuck on "stages appear here as they start reporting" for the rest of the run --
          the durable current_stage (user-reported live, thread 8242ea6d: reattached at Remediation,
          Overview still showed nothing) already proves everything before it finished. Uses
          boundaryIdx (not raw current_stage) so a genuine failure's mapped target -- possibly
          EARLIER than current_stage, see the boundary comment above -- decides the split. */}
      {stages.length === 0 && boundaryIdx < 0 && (
        <p className="text-sm text-neutral-500">
          {runActivity?.interrupted
            ? "This run appears to have stopped before any stage reported in. Resume to pick it back up."
            : events.length > 0 && runActivity?.runActive !== false
              ? "Run in progress — stages appear here as they start reporting."
              : "No stages have run yet."}
        </p>
      )}

      {stages.length === 0 && boundaryIdx >= 0 && (
        <div className="flex flex-col">
          <div className={`${ROW_GRID} px-4 pb-1 text-xs font-medium text-neutral-400`}>
            <span>Stage</span>
            <span className="text-right">Duration</span>
            <span className="text-right">Cost</span>
            <span className="text-right">Redrafts</span>
            <span className="text-right">Status</span>
          </div>
          <ol className="flex flex-col gap-2">
            {PIPELINE_STAGE_ORDER.slice(0, boundaryIdx + 1).map((s, i, arr) => {
              // Only the LAST (boundary) row is ever ambiguous -- every earlier one is durably
              // known complete, so it keeps the generic sync-pending copy. The boundary row needs
              // real status: EITHER the real failure (durable failureType/failureMessage + the one
              // restart action, root-caused 2026-09-12), OR -- when nothing actually failed --
              // current_stage's own ambiguity (it advances both on approval AND on a stage's own
              // draft START, graph.py's make_draft_node, so "current_stage == X" can mean "X
              // finished" OR "X itself died mid-draft" with no durable way to tell apart for a
              // non-gated stage). Never claim "Completed" here in that case -- targeting X itself
              // for the Continue action is always safe (worst case: a harmless redraft of a stage
              // that had actually finished); claiming it's done and jumping past it is not.
              const isBoundary = i === arr.length - 1;
              const running = isBoundary && !isFailedBoundary && runActivity?.runActive !== false;
              let label: string;
              if (!isBoundary) {
                label = "Completed — waiting for full detail to sync…";
              } else if (isFailedBoundary) {
                label = "Failed";
              } else if (finishedWithVerdict) {
                label = "Approved — see the Report tab for the full verdict";
              } else if (running) {
                label = "Running";
              } else {
                label = "Last reached — click Continue to pick up from here";
              }
              return (
                <li
                  key={s.key}
                  className={`rounded-lg border px-4 py-2 text-sm ${
                    isBoundary && isFailedBoundary ? "border-red-300 bg-red-50" : "border-neutral-200"
                  }`}
                >
                  <div className={ROW_GRID}>
                    <span className="font-medium">{s.label}</span>
                    <span />
                    <span />
                    <span />
                    <span
                      className={`flex items-center justify-end gap-1.5 ${
                        isBoundary && isFailedBoundary ? "text-red-700" : "text-neutral-500"
                      }`}
                    >
                      {running && <RunningSpinner />}
                      {label}
                    </span>
                  </div>
                  {isBoundary && !finishedWithVerdict && (isFailedBoundary || !running) && (
                    <div className="mt-2 flex items-start justify-between gap-3 border-t border-neutral-100 pt-2">
                      {isFailedBoundary && (failureTypeText || failureMessageText) && (
                        <p className="text-xs text-red-700">
                          {failureTypeText}
                          {failureTypeText && failureMessageText ? " — " : ""}
                          {failureMessageText}
                        </p>
                      )}
                      <button
                        type="button"
                        className={`ml-auto shrink-0 rounded-md px-3 py-1 text-xs font-medium disabled:opacity-40 ${
                          isFailedBoundary
                            ? "bg-neutral-900 text-white"
                            : "border border-neutral-300 bg-white text-neutral-700"
                        }`}
                        disabled={restarting}
                        onClick={() => void handleRestart(s.key)}
                      >
                        {restarting
                          ? "Working…"
                          : isFailedBoundary
                            ? "Restart workflow from this stage"
                            : "Continue workflow from here"}
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {stages.length > 0 && (
        <div className="flex flex-col">
          <div className={`${ROW_GRID} px-4 pb-1 text-xs font-medium text-neutral-400`}>
            <span>Stage</span>
            <span className="text-right">Duration</span>
            <span className="text-right">Cost</span>
            <span className="text-right">Redrafts</span>
            <span className="text-right">Status</span>
          </div>
          <ol className="flex flex-col gap-2">
            {stages.map(([key, stage]) => {
              const timing = perStage.get(key);
              const note = stageNote(stage);
              const failedHere = failure?.stage === key;
              // `stage.status` alone misses non-gated stages (ac-to-tests, minimal-code-to-green,
              // remediation, ...): it only updates when the run pauses at a gate, so a stage with
              // no gate can sit at "not_started" for its entire real drafting time. `timing.running`
              // (perStage, above) is derived from the live event stream instead, which has no such
              // lag -- OR'd together since state is never wrong when it does say "drafting". Gated
              // on runActive !== false too (Workflow Liveness Fix): a killed process left mid-draft
              // leaves `status === "drafting"` forever, which used to read as running with no other
              // signal to contradict it.
              const running = (stage.status === "drafting" && runActivity?.runActive !== false) || timing?.node !== undefined;
              // Pivot (root-caused 2026-09-12): the one restart/continue action, on whichever row
              // is the durable boundary -- computed once above from durable data, so it lands on
              // the correct row (the mapped real stage, e.g. "ac-to-tests" for an
              // r_ac_to_tests rebuild failure) even with live per-stage state also available.
              // Never shown while genuinely still running -- nothing to continue then.
              const isBoundaryRow = key === boundaryKey;
              const showAction = isBoundaryRow && !finishedWithVerdict && (isFailedBoundary || !running);
              // At most one placement follows any given real stage today (REBUILD_PLACEMENTS has
              // no two entries sharing an afterStageKey) -- find(), not filter().
              const placement = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === key);
              const phase = placement && rebuildPhase(state, placement, runActivity?.runActive, runningPhases);
              const rebuildRow = placement && phase && (
                <RebuildRow
                  key={placement.rebuildKey}
                  placement={placement}
                  phase={phase}
                  timing={rebuildTimings.get(placement.rebuildKey)}
                  failedHere={failure?.stage === placement.rebuildKey}
                />
              );
              return (
                <Fragment key={key}>
                <li
                  className={`rounded-lg border px-4 py-2 text-sm ${
                    failedHere ? "border-red-300 bg-red-50" : "border-neutral-200"
                  }`}
                >
                  <div className={ROW_GRID}>
                    <span className="font-medium">{PIPELINE_STAGE_ORDER.find((s) => s.key === key)?.label ?? key}</span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing && timing.last > timing.first ? formatDuration(timing.last - timing.first) : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing?.sawCost ? `$${timing.cost.toFixed(2)}` : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing && timing.rejections > 0 ? `${timing.rejections}×` : ""}
                    </span>
                    <span
                      className={`flex items-center justify-end gap-1.5 ${failedHere ? "text-red-700" : "text-neutral-500"}`}
                    >
                      {running && <RunningSpinner />}
                      {failedHere
                        ? "Failed"
                        : running
                          ? (timing?.node ? (NODE_PHASE_LABEL[timing.node] ?? "Running") : "Drafting")
                          : (STATUS_LABEL[stage.status] ?? stage.status)}
                    </span>
                  </div>
                  {note && <p className="mt-1 text-xs text-neutral-500">{note}</p>}
                  {showAction && (
                    <div className="mt-2 flex items-start justify-between gap-3 border-t border-neutral-100 pt-2">
                      {isFailedBoundary && (failureTypeText || failureMessageText) && (
                        <p className="text-xs text-red-700">
                          {failureTypeText}
                          {failureTypeText && failureMessageText ? " — " : ""}
                          {failureMessageText}
                        </p>
                      )}
                      <button
                        type="button"
                        className={`ml-auto shrink-0 rounded-md px-3 py-1 text-xs font-medium disabled:opacity-40 ${
                          isFailedBoundary
                            ? "bg-neutral-900 text-white"
                            : "border border-neutral-300 bg-white text-neutral-700"
                        }`}
                        disabled={restarting}
                        onClick={() => void handleRestart(key)}
                      >
                        {restarting
                          ? "Working…"
                          : isFailedBoundary
                            ? "Restart workflow from this stage"
                            : "Continue workflow from here"}
                      </button>
                    </div>
                  )}
                  {key === "metrics-exit" && finishedWithVerdict && runActivity?.mergeReady === false && (
                    <div className="mt-2 flex items-start justify-between gap-3 border-t border-neutral-100 pt-2">
                      <p className="text-xs text-neutral-500">
                        This run finished but is not ready to merge -- see the Report tab for why.
                        Ask the agent to fix it, or re-verify if you've already fixed it yourself.
                      </p>
                      <div className="ml-auto flex shrink-0 gap-2">
                        <button
                          type="button"
                          className="rounded-md border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 disabled:opacity-40"
                          disabled={restarting}
                          onClick={() => void handleReverifyMetricsExit()}
                        >
                          {restarting ? "Working…" : "Re-verify Metrics & Exit"}
                        </button>
                        <button
                          type="button"
                          className="rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
                          disabled={restarting}
                          onClick={() => void handleTargetedFix()}
                        >
                          {restarting ? "Working…" : "Fix these findings"}
                        </button>
                      </div>
                    </div>
                  )}
                </li>
                {rebuildRow}
                </Fragment>
              );
            })}
          </ol>
        </div>
      )}
    </ViewContainer>
  );
}
