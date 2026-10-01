"use client";

import { UseAgentUpdate, useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { useMemo, useState } from "react";
import { usePipeline } from "@/lib/pipeline";
import { useRunActivity } from "@/lib/run-activity-context";
import { useSessionSummary } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { RebuildPlacement, WorkflowState } from "@/lib/workflow-types";

// The failed-run recovery panel and the restart logic behind it, moved out of SessionOverview
// (2026-10-01, user request: a failed stage's own tab must offer the fix, not point at Overview)
// so Overview and every other tab render the SAME component -- still exactly one implementation of
// "the one explicit action that advances the graph" (SessionOverview's pivot note).

/** Plain-language failure summary for the recovery panel (root-caused 2026-09-13, user-reported:
 * the raw `failureTypeText` enum, e.g. "cannot_verify", was shown to users completely unexplained).
 * `rawFailureStage` is the PRE-`realStageForFailure` value (`failure.stage`/`runActivity.
 * failureStage`) -- often a rebuild_placements `rebuildKey` (e.g. "r_ac_to_tests", set by
 * agent/src/rebuild.py's own make_escalate_node), not a real stage key. Using the placement's own
 * label ("Red Gate") when it matches one avoids misattributing a rebuild-gate's own build-check
 * failure to the real stage's content/audit, which is already approved and uninvolved. */
function describeFailure(
  rawFailureStage: string | null,
  failureTypeText: string | null,
  failureMessageText: string | null,
  mappedStageLabel: string,
  placements: RebuildPlacement[],
): string {
  const placement = placements.find((p) => p.rebuildKey === rawFailureStage);
  const label = placement?.label ?? mappedStageLabel;
  if (failureTypeText === "cannot_verify") {
    return `${label}'s last check couldn't run because no sandbox was available — this is an infrastructure hiccup, not a problem with your code.`;
  }
  return failureMessageText ? `${label} hit a problem: ${failureMessageText}` : `${label} hit a problem.`;
}

/** Plain-language "what a redo actually costs" copy, built from `stagesResetByRewind`'s own output.
 * Degrades gracefully when `labels` is empty (the durable-only fallback view, which has no live
 * per-stage data to enumerate) -- never renders an empty "— —" fragment. */
function buildRedoCopy(stageLabel: string, labels: string[], approxCost: number): string {
  const parts = [`This resets ${stageLabel} and everything after it`];
  if (labels.length > 0) parts.push(`— ${labels.join(", ")} —`);
  parts.push("and redoes them from scratch");
  if (labels.length > 0) {
    parts.push(`(≈$${approxCost.toFixed(2)} so far across those stages this run, based on the most recent attempt at each)`);
  }
  return `${parts.join(" ")}. Files already written are not reverted.`;
}

/** Ensures a sandbox is registered for `threadId` before invoking any recovery action below
 * (rewind-to-stage, reverify-metrics-exit, targeted-fix, resume-stuck-e2e) -- closes the "silent
 * no-op" gap where `_run_targeted_fix`/`deterministic_verify` (agent/src/graph.py) both bail
 * quietly when `sandbox_registry.get(thread_id) is None`, which is exactly the state a finished or
 * long-idle session's container is usually in by the time a user reaches this tab.
 *
 * `confirmReopen: true` mirrors the same-named registry meta flag these actions already set via
 * POST /api/sessions/actions (sessions_api.py:929-933, 958-960) -- without it, `/api/sessions/
 * provision`'s own `is_finished_with_verdict` guard 409s for exactly the finished, merge_ready=false
 * sessions these actions exist to act on. Inert (and harmless) for a session that isn't finished
 * with a verdict, e.g. Change 5's stuck-e2e case below -- that guard never fires for it either way.
 *
 * Best-effort: on failure, the action's own POST below still fires and reports whatever error the
 * agent itself surfaces (a missing sandbox becomes an explicit "no sandbox" failure there rather
 * than this helper silently blocking the whole action on a provisioning hiccup). */
export async function ensureSandboxProvisioned(threadId: string, owner: string, repo: string, branch: string): Promise<void> {
  try {
    await fetch("/api/sessions/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: threadId, owner, repo, branch, resume: true, confirmReopen: true }),
    });
  } catch {
    // Best-effort -- see this function's own doc.
  }
}

export function useRecovery(owner: string, repo: string, branch: string) {
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const { copilotkit } = useCopilotKit();
  const state = (agent.state ?? {}) as WorkflowState;
  const pipeline = usePipeline();
  const { stageOrderIndex, realStageForFailure, rebuildPlacements, retriesInPlace } = pipeline;
  // Real run order then legacy keys -- index i here is exactly stageOrderIndex(key).
  const stageOrder = useMemo(
    () => [...pipeline.order, ...Object.keys(pipeline.descriptor.legacy_labels)].map((key) => ({ key, label: pipeline.stageLabel(key) })),
    [pipeline],
  );
  // Root-caused 2026-09-12 (user-reported: stages rendered out of pipeline order): `state.stages`
  // is a plain object -- Object.entries has no ordering guarantee of its own, only whatever order
  // the backend happened to insert keys in. Sort by the same pipeline run order every other
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
  const orderedRealStages = stageOrder.filter((s) => state.stages?.[s.key] != null);
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
  // Exposed separately from mappedFailureTarget (root-caused 2026-09-13): this is the PRE-mapped
  // value -- often a rebuild_placements rebuildKey, not a real stage key -- needed by
  // describeFailure below to attribute a rebuild-gate's own failure to the placement itself
  // ("Red Gate") rather than the real stage it's mapped to for row/rewind-targeting purposes.
  const rawFailureStage = hasLiveStages
    ? (failure ? (failure.stage ?? null) : null)
    : (runActivity?.status === "failed" && !runActivity?.finishedWithVerdict ? (runActivity?.failureStage ?? null) : null);
  const mappedFailureTarget = rawFailureStage != null ? realStageForFailure(rawFailureStage) : null;
  // Boundary = where "last known-good" ends. A genuine failure's mapped target is the TRUE
  // boundary: current_stage can be pushed all the way to metrics-exit by the crash-reporting pass
  // that still runs after most escalates, which would otherwise misreport "everything through
  // Metrics & Exit succeeded" (confirmed against graph.py: metrics-exit_draft's own draft-start
  // write bumps current_stage regardless of why it was reached). When nothing failed (or the
  // session finished with a real verdict), current_stage is fully trustworthy as-is.
  const boundaryKey = mappedFailureTarget ?? (hasLiveStages ? firstNonApprovedKey : (runActivity?.currentStage ?? null));
  const boundaryIdx = stageOrderIndex(boundaryKey);
  const isFailedBoundary = mappedFailureTarget != null;
  const boundaryLabel = stageOrder.find((s) => s.key === boundaryKey)?.label ?? boundaryKey ?? "This stage";
  // The "redo a different stage instead" disclosure's own candidate list (root-caused 2026-09-13
  // UX redesign): every OTHER stage this run has genuinely approved, excluding the boundary --
  // `stage.status === "approved"` is the same live, trustworthy reachability proof
  // `sessions_api.py`'s rewind-to-stage endpoint itself now accepts (falls back to it whenever the
  // durable current_stage column disagrees). Naturally empty in the durable-only fallback view
  // (state.stages has no live per-stage data yet there) -- nothing to offer alternatives from, so
  // the disclosure correctly renders nothing rather than guessing.
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
  // Cheap infra-only retry (root-caused 2026-09-13, "FE must show true live state" investigation):
  // `cannot_verify` means the check never actually ran -- no sandbox was available at the moment a
  // deterministic verify/rebuild gate tried to run (make_escalate_node and rebuild.py's own
  // escalate_node both tag it identically) -- never that the stage's own content was judged and
  // found wanting. The stage this failure is mapped to is still `approved`; nothing about it needs
  // redoing. intake_node's reopen guard doesn't even apply here either: it only blocks
  // `is_finished_with_verdict` sessions (status=="completed", or failed with failure_stage=="exit"),
  // and this shape is neither -- a genuine crash with any other failure_stage already "falls
  // through to the ordinary, unconfirmed resume path" (that guard's own comment). So a bare resume,
  // once a sandbox actually exists, naturally re-enters the graph, finds the mapped stage still
  // approved, and retries just the check that never ran -- no rewind-to-stage call, no redraft.
  const isCannotVerifyBoundary = isFailedBoundary && failureTypeText === "cannot_verify";
  // The server declares which failures a plain resume retries IN PLACE (descriptor
  // retry_in_place_failures: a rebuild check resets its own fix budget when it escalates), so a
  // red-gate failure gets the cheap "Retry this check" first; the full redo of the boundary stage
  // stays available under "Redo a different stage instead".
  const isRetryBoundary = isCannotVerifyBoundary || (isFailedBoundary && retriesInPlace(rawFailureStage));
  const otherApprovedStages = isFailedBoundary
    ? stageOrder.filter((s) => (isRetryBoundary || s.key !== boundaryKey) && state.stages?.[s.key]?.status === "approved")
    : [];
  // `redo`: the explicit full redo of a stage, even the boundary when it could be retried in place.
  async function handleRestart(stageKey: string, redo = false) {
    const label = stageOrder.find((s) => s.key === stageKey)?.label ?? stageKey;
    // Only the boundary's OWN stage qualifies for the cheap retry -- a user who explicitly picked
    // a DIFFERENT (earlier) stage via its own row's button is asking for a real rewind to THAT
    // stage, which still needs the full reset regardless of why the boundary itself failed.
    if (!redo && isRetryBoundary && stageKey === boundaryKey) {
      if (
        !window.confirm(
          isCannotVerifyBoundary
            ? "This failed because no sandbox was available to run the check -- nothing about " +
                `${label}'s own work was judged and found wrong. Retrying just re-runs that check ` +
                "against a fresh sandbox; it does not reset or redo any stage. Continue?"
            : "This re-runs only the check that failed, against the current code, with a fresh fix " +
                `budget -- ${label} and every other approved stage are reused as-is, not redone. Continue?`,
        )
      ) {
        return;
      }
      setRestarting(true);
      try {
        await ensureSandboxProvisioned(threadId, owner, repo, branch);
        void copilotkit.runAgent({ agent });
      } finally {
        setRestarting(false);
      }
      return;
    }
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
        await ensureSandboxProvisioned(threadId, owner, repo, branch);
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
      await ensureSandboxProvisioned(threadId, owner, repo, branch);
      void copilotkit.runAgent({ agent });
    } finally {
      setRestarting(false);
    }
  }

  // Server-computed per-stage/per-rebuild-placement duration+cost (Overview-tab fix, 2026-09-22):
  // replaces this tab re-deriving those two numbers from the full held event array on every
  // render, and is what makes a rebuild placement's own duration/cost correct for the first time
  // (see agent/src/run_event_summary.py + agent/src/rebuild.py's retag). Polls only while the run
  // is active.
  const summary = useSessionSummary(threadId, runActivity?.runActive);

  // What a rewind to `stageKey` actually resets (root-caused 2026-09-13 UX redesign): mirrors
  // agent/src/graph.py:2515-2542's own rewind-to-stage handling exactly -- every real stage from
  // `stageKey` onward, plus every rebuild_placements entry whose `afterStageKey` sits at or after
  // it (graph.py:2536-2538) -- so the enumerated label list and cost match the real consequence,
  // not an approximation. `state.stages?.[s.key] == null` filters out the pipeline order's own
  // legacy/never-populated tail keys (brownfield-baseline, raw-requirements, and four retired
  // stage keys the current graph never writes -- see that file's own comment) so the confirm copy
  // never lists a phantom stage. `summary` (server-computed, Overview-tab fix 2026-09-22) is
  // keyed by the same normalized stage/placement key either way, so each stage's own trailing
  // placement (if any) must still be looked up via rebuild_placements explicitly -- a direct
  // `summary.get(s.key)` for the placement itself would silently always miss.
  function stagesResetByRewind(stageKey: string): { labels: string[]; approxCost: number } {
    const startIdx = stageOrderIndex(stageKey);
    const labels: string[] = [];
    let approxCost = 0;
    for (const s of stageOrder) {
      if (stageOrderIndex(s.key) < startIdx) continue;
      if (state.stages?.[s.key] == null) continue;
      labels.push(s.label);
      approxCost += summary.get(s.key)?.cost ?? 0;
      const placement = rebuildPlacements.find((p) => p.afterStageKey === s.key);
      if (placement) approxCost += summary.get(placement.rebuildKey)?.cost ?? 0;
    }
    return { labels, approxCost };
  }

  return {
    localAgentId, threadId, agent, copilotkit, state, pipeline, stageOrderIndex, realStageForFailure, rebuildPlacements, stageOrder, stages, failure, runActivity, hasLiveStages, orderedRealStages, firstNonApprovedKey, rawFailureStage, mappedFailureTarget, boundaryKey, boundaryIdx, isFailedBoundary, boundaryLabel, otherApprovedStages, failureTypeText, failureMessageText, finishedWithVerdict, restarting, setRestarting, isCannotVerifyBoundary, isRetryBoundary, handleRestart, summary, stagesResetByRewind,
  };
}

export type Recovery = ReturnType<typeof useRecovery>;

/** The failed-run recovery panel; renders nothing unless the run failed at a mappable stage. */
export function RecoveryPanel({ recovery }: { recovery: Recovery }) {
  const {
    isFailedBoundary, mappedFailureTarget, rawFailureStage, failureTypeText, failureMessageText, boundaryLabel,
    rebuildPlacements, isRetryBoundary, stagesResetByRewind, restarting, handleRestart, otherApprovedStages,
  } = recovery;
  if (!isFailedBoundary || !mappedFailureTarget) return null;
  return (
      <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
        <p className="font-medium">
          {describeFailure(rawFailureStage, failureTypeText, failureMessageText, boundaryLabel, rebuildPlacements)}
        </p>
        {(failureTypeText || failureMessageText) && (
          <details className="mt-1">
            <summary className="cursor-pointer text-xs text-red-700">Show technical details</summary>
            <p className="mt-1 text-xs text-red-700">
              {failureTypeText}
              {failureTypeText && failureMessageText ? " — " : ""}
              {failureMessageText}
            </p>
          </details>
        )}

        <div className="mt-3 flex items-start justify-between gap-3">
          <p className="text-xs text-red-800">
            {isRetryBoundary
              ? "This only retries the failed check. Any stage already marked Approved above is reused as-is, not redone."
              : (() => {
                  const { labels, approxCost } = stagesResetByRewind(mappedFailureTarget);
                  return buildRedoCopy(boundaryLabel, labels, approxCost);
                })()}
          </p>
          <button
            type="button"
            className="shrink-0 rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            disabled={restarting}
            onClick={() => void handleRestart(mappedFailureTarget)}
          >
            {restarting ? "Working…" : isRetryBoundary ? "Retry this check" : `Redo ${boundaryLabel}`}
          </button>
        </div>

        {otherApprovedStages.length > 0 && (
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium text-red-800">Redo a different stage instead</summary>
            <ol className="mt-2 flex flex-col gap-2">
              {otherApprovedStages.map((s) => {
                const { labels, approxCost } = stagesResetByRewind(s.key);
                return (
                  <li
                    key={s.key}
                    className="flex items-center justify-between gap-3 rounded-md border border-red-200 bg-white px-3 py-2"
                  >
                    <span className="text-xs text-red-900">
                      {s.label} — resets {labels.length} stage{labels.length === 1 ? "" : "s"} (≈${approxCost.toFixed(2)} so far)
                    </span>
                    <button
                      type="button"
                      className="shrink-0 rounded-md border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 disabled:opacity-40"
                      disabled={restarting}
                      onClick={() => void handleRestart(s.key, true)}
                    >
                      {restarting ? "Working…" : "Redo from here"}
                    </button>
                  </li>
                );
              })}
            </ol>
          </details>
        )}
      </div>
  );
}
