"use client";

import { useAgent, useCopilotKit } from "@copilotkit/react-core/v2";
import { Fragment, useMemo, useState, type MouseEvent } from "react";
import { createPortal } from "react-dom";
import { RunningSpinner } from "@/components/Spinner";
import { ViewContainer } from "@/components/ViewContainer";
import { useRunActivity } from "@/lib/run-activity-context";
import {
  computeRunningPhases,
  NODE_PHASE_LABEL,
  formatDuration,
  useSessionSummary,
  useStructuralRunEvents,
  type RunLogEvent,
  type StageSummary,
} from "@/lib/use-run-events";
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

// User-requested (2026-09-22): a colored status dot per stage/placement row, so a passed/approved
// row reads as green at a glance instead of every status looking the same plain neutral text.
// Computed from the SAME raw booleans/status values each row already branches its label text on
// (failedHere/running/stage.status or phase.status), never from the already-localized label
// string -- string-matching rendered text is fragile (a copy change silently breaks the color) and
// this file already has every one of those raw values in scope at each call site.
type StatusTone = "green" | "red" | "amber" | "gray";

const STATUS_DOT_CLASS: Record<StatusTone, string> = {
  green: "bg-emerald-500",
  red: "bg-red-500",
  amber: "bg-amber-500",
  gray: "bg-neutral-300",
};

function StatusDot({ tone }: { tone: StatusTone }) {
  return <span aria-hidden className={`inline-block h-2 w-2 shrink-0 rounded-full ${STATUS_DOT_CLASS[tone]}`} />;
}

/** The Overview tab: session cost + a per-stage table (duration, spend, redraft count, status).
 * Timeline (Swimlane.tsx) and the detailed event log (EventLogView.tsx) were removed here (user
 * request 2026-09-01) along with their now-dead component files (and DiffView.tsx, which existed
 * only to render EventLogView's diff payloads) -- this was the only place either was mounted. */

/** One stage's short human-facing note: the live failure feedback while it's failing, else the
 * approved summary. Never the raw draft. The "N audit finding(s) addressed" case is rendered
 * separately (AuditFindingsNote below, user-requested tooltip listing each finding) rather than
 * collapsed into this plain string, so it's excluded here -- same priority order as before
 * (failure text wins over an approved summary), just missing the middle rung. */
function stageNote(stage: StageState): string | null {
  const v = stage.last_verification;
  if (v && !v.passed && stage.status !== "approved") return truncate(v.feedback, 140);
  const summary = (stage.approved_content as { summary?: string } | null)?.summary;
  if (stage.status === "approved" && summary) return truncate(summary, 140);
  return null;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** "N audit finding(s) addressed", hoverable to reveal the actual findings (user-requested,
 * 2026-09-17) -- portal-positioned the same way IoPreviewCell's tooltip is (fixed, computed from
 * the trigger's own bounding rect) so it can never be clipped by a scrolling/overflow ancestor. */
function AuditFindingsNote({ findings }: { findings: string[] }) {
  const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null);

  function handleEnter(e: MouseEvent<HTMLParagraphElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    setAnchor({ top: r.bottom + 4, left: r.left });
  }

  return (
    <p
      className="mt-1 w-fit cursor-default text-xs text-neutral-500 underline decoration-dotted decoration-neutral-300 underline-offset-2"
      onMouseEnter={handleEnter}
      onMouseLeave={() => setAnchor(null)}
    >
      {findings.length} audit finding(s) addressed
      {anchor &&
        createPortal(
          <ul className="fixed z-50 max-h-64 w-96 list-disc overflow-auto whitespace-pre-wrap rounded-md border border-neutral-200 bg-white p-2 pl-6 text-left text-xs normal-case leading-relaxed text-neutral-700 shadow-lg"
            style={{ top: anchor.top, left: anchor.left }}
          >
            {findings.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>,
          document.body,
        )}
    </p>
  );
}

/** Plain-language failure summary for the recovery panel (root-caused 2026-09-13, user-reported:
 * the raw `failureTypeText` enum, e.g. "cannot_verify", was shown to users completely unexplained).
 * `rawFailureStage` is the PRE-`realStageForFailure` value (`failure.stage`/`runActivity.
 * failureStage`) -- often a REBUILD_PLACEMENTS `rebuildKey` (e.g. "r_ac_to_tests", set by
 * agent/src/rebuild.py's own make_escalate_node), not a real stage key. Using the placement's own
 * label ("Red Gate") when it matches one avoids misattributing a rebuild-gate's own build-check
 * failure to the real stage's content/audit, which is already approved and uninvolved. */
function describeFailure(
  rawFailureStage: string | null,
  failureTypeText: string | null,
  failureMessageText: string | null,
  mappedStageLabel: string,
): string {
  const placement = REBUILD_PLACEMENTS.find((p) => p.rebuildKey === rawFailureStage);
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

/** The single "just continue, nothing failed" affordance (root-caused 2026-09-13: this used to be
 * hand-duplicated in both the live and durable-fallback rendering branches below, and a rename
 * applied to only one of them would have silently left the other stale -- exactly the class of bug
 * this redesign exists to close). Shared by both branches so a future change can't do that again. */
function ContinueAction({ restarting, onClick }: { restarting: boolean; onClick: () => void }) {
  return (
    <div className="mt-2 flex items-start justify-between gap-3 border-t border-neutral-100 pt-2">
      <button
        type="button"
        className="ml-auto shrink-0 rounded-md border border-neutral-300 bg-white px-3 py-1 text-xs font-medium text-neutral-700 disabled:opacity-40"
        disabled={restarting}
        onClick={onClick}
      >
        {restarting ? "Working…" : "Resume this run"}
      </button>
    </div>
  );
}

// Shared between the header row and every stage row so the columns actually line up like a table
// (user feedback 2026-09-01) instead of each row's flex layout drifting with its own content width.
// Column widths rebalanced 2026-09-17 (user-reported: Redraft History's 4-column subtable forced
// a horizontal scrollbar) -- Duration/Cost/Redrafts/Status shed 3rem total (each still comfortably
// fits its actual longest value: "Needs clarification" for Status, "N×" for Redrafts, "$NN.NN" /
// "NNm NNs" for Cost/Duration), handed to Redraft History so its 4 sub-columns (Lap, Draft/Audit,
// two byte sizes) fit without overflowing -- same total fixed width as before, just redistributed.
const ROW_GRID = "grid grid-cols-[1fr_4rem_3.5rem_3.5rem_8.5rem_14rem] items-center gap-3";

/** One REBUILD_PLACEMENTS row, inserted right after its `afterStageKey`'s own row (rebuildPhase's
 * own docstring: real, unattributed-to-a-single-placement work happening between two stages).
 * `timing` (Overview-tab fix, 2026-09-22) is looked up directly from the server-computed summary
 * (agent/src/run_event_summary.py), keyed by `placement.rebuildKey` -- that key is exactly what
 * agent/src/rebuild.py now tags every one of a placement's own events with (span events AND, as
 * of this fix, its inner discovery/red-gate/fix turns), so no client-side time-windowing between
 * neighboring real stages is needed any more. `undefined` means the placement has no events for
 * its own latest run_id yet (never entered, or a resume hasn't reached it again) -- still-running
 * placements now show a live, growing duration each summary poll instead of staying blank. */
function RebuildRow({
  placement,
  phase,
  timing,
  failedHere,
}: {
  placement: RebuildPlacement;
  phase: { status: "not_started" | "clean" | "failed" | "fixing"; running: boolean };
  timing: StageSummary | undefined;
  failedHere: boolean;
}) {
  return (
    <li className={`rounded-lg border px-4 py-2 text-sm ${failedHere ? "border-red-300 bg-red-50" : "border-neutral-200"}`}>
      <div className={ROW_GRID}>
        <span className="font-medium">{placement.label}</span>
        <span className="text-right text-xs text-neutral-500">
          {/* Defensive guard only now (duration comes from the server-computed summary, which is
              already correct) -- kept as cheap display-time protection, same as the real-stage
              row's own identical guard a screen down. */}
          {timing && timing.last > timing.first ? formatDuration(timing.last - timing.first) : ""}
        </span>
        <span className="text-right text-xs text-neutral-500">{timing?.costKnown ? `$${timing.cost.toFixed(2)}` : ""}</span>
        <span className="text-right text-xs text-neutral-500" />
        <span className={`flex items-center justify-end gap-1.5 ${failedHere ? "text-red-700" : "text-neutral-500"}`}>
          <StatusDot tone={failedHere ? "red" : phase.running ? "amber" : phase.status === "clean" ? "green" : phase.status === "fixing" ? "amber" : "gray"} />
          {phase.running && <RunningSpinner />}
          {failedHere ? "Failed" : phase.running ? "Verifying" : REBUILD_STATUS_LABEL[phase.status]}
        </span>
        {/* Redraft History column: rebuild placements aren't real stages (no perStage entry of
            their own), so there's nothing to show here -- an empty cell keeps the grid aligned. */}
        <span />
      </div>
    </li>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

type EventIoText = { input_text: string | null; output_text: string | null };

// Module-level, shared across every mounted IoPreviewCell -- dbo.run_events rows are append-only
// (Workstream 3: a lap's input/output text is written once at NODE_FINISHED and never revised),
// so caching by "sessionId:seq" forever for the life of this tab is safe: re-hovering the same
// cell, or a re-render, never needs to refetch.
const ioTextCache = new Map<string, EventIoText>();

async function fetchEventIo(sessionId: string, seq: number): Promise<EventIoText | null> {
  const key = `${sessionId}:${seq}`;
  const cached = ioTextCache.get(key);
  if (cached) return cached;
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/events/${seq}/io`);
  if (!res.ok) return null;
  const data = (await res.json()) as EventIoText;
  ioTextCache.set(key, data);
  return data;
}

/** One Input/Output cell in the redraft-history subtable: shows the byte size always, fetches
 * and shows the FULL text only on hover. Deliberately not the plain `title` attribute every other
 * tooltip in this app uses (MetricsBar's Chip, `title={title}`) -- that's a static string already
 * known at render time, which can't work here since the whole point of the split list/on-demand
 * API (sessions_api.py's RunEventResponse vs. RunEventIoResponse) is to NOT eagerly download every
 * lap's full prompt/response just to render a size number nobody may ever hover over.
 *
 * Popup renders through a portal to `document.body`, positioned from the trigger's own
 * `getBoundingClientRect()` (root-caused 2026-09-17, user-reported "weird display"/scroll-arrow
 * glitch on hover): this cell lives inside RedraftHistoryCell's `max-h-24 overflow-y-auto` list,
 * and a merely `absolute`-positioned popup is still clipped by that scrolling ancestor -- the
 * popup's own overflow forced the tiny row list into scroll mode instead of floating above it.
 * `position: fixed` + a portal is the standard escape for a tooltip inside a scroll container. */
function IoPreviewCell({
  sessionId,
  seq,
  size,
  field,
}: {
  sessionId: string;
  seq: number;
  size: number | null;
  field: keyof EventIoText;
}) {
  const [text, setText] = useState<string | null>();
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);

  function handleEnter(e: MouseEvent<HTMLSpanElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    setAnchor({ top: r.bottom + 4, right: window.innerWidth - r.right });
    if (text !== undefined) return; // already fetched (or already known-absent) -- don't refetch
    void fetchEventIo(sessionId, seq).then((io) => setText(io ? io[field] : null));
  }

  if (size == null) return <span className="w-12 shrink-0 text-right text-neutral-300">—</span>;
  return (
    <span
      className="w-12 shrink-0 cursor-default text-right underline decoration-dotted decoration-neutral-300 underline-offset-2"
      onMouseEnter={handleEnter}
      onMouseLeave={() => setAnchor(null)}
    >
      {formatBytes(size)}
      {anchor &&
        createPortal(
          <div
            className="fixed z-50 max-h-64 w-80 overflow-auto whitespace-pre-wrap rounded-md border border-neutral-200 bg-white p-2 text-left text-[11px] normal-case leading-relaxed text-neutral-700 shadow-lg"
            style={{ top: anchor.top, right: anchor.right }}
          >
            {text === undefined ? "Loading…" : (text ?? "Not available")}
          </div>,
          document.body,
        )}
    </span>
  );
}

/** One stage's ordered redraft history: Lap | Node | Input | Output, one row per completed
 * draft/audit/fix call. `redrafts` (perStage's own array, above) is already in accurate execution
 * order -- events arrive oldest-first and seq is a durable monotonic IDENTITY, so no re-sort is
 * needed here. Compact by design: this sits inside one Overview-table cell, not its own page.
 *
 * Labeled "Lap N" (1-based), derived from this ARRAY's own position, not from `e.payload.cycle`
 * (`verify_cycle_count`, graph.py). Root-caused 2026-09-17, user-reported "weird numbering and
 * order": `verify_cycle_count` is in-memory graph state, not a durable-across-restarts counter --
 * resuming a stage after an agent-process restart can reset it to 0, so a chronologically LATER
 * event (still correctly ordered here by `seq`, a real monotonic id) can report a LOWER cycle
 * than an earlier one already in this same list ("Lap 1 Draft" appearing after "Lap 4 Draft").
 * Counting a new lap every time a "draft" node appears in this already-correctly-ordered array
 * is immune to that reset -- a draft and the audit that follows it in the same lap still
 * deliberately share one number.
 *
 * Click-to-expand (memory/render-cost fix, 2026-09-22): was unconditionally rendered in full for
 * every stage that had any redraft history at all -- real cost on a long session with many laps
 * across many stages. Collapsed by default; expanding is a plain click, no data refetch (the
 * `redrafts` array is already held by the caller either way -- only the DOM cost of rendering
 * every row was deferred, per-lap IO text itself was already fetch-on-hover). */
function RedraftHistoryCell({ sessionId, redrafts }: { sessionId: string; redrafts: RunLogEvent[] }) {
  const [expanded, setExpanded] = useState(false);
  if (redrafts.length === 0) return null;
  const lapCount = redrafts.filter((e) => e.node === "draft").length;
  if (!expanded) {
    return (
      <button
        type="button"
        className="w-fit cursor-default text-left text-[11px] text-neutral-500 underline decoration-dotted decoration-neutral-300 underline-offset-2"
        onClick={() => setExpanded(true)}
      >
        {lapCount} lap{lapCount === 1 ? "" : "s"} — click to expand
      </button>
    );
  }
  // Lap numbers computed in a plain loop, not inside the JSX-producing .map() below -- a mutable
  // counter reassigned inside a render callback trips this codebase's react-hooks/immutability
  // lint rule.
  const lapNumbers: number[] = [];
  let lap = 0;
  for (const e of redrafts) {
    if (e.node === "draft") lap += 1;
    lapNumbers.push(lap);
  }
  return (
    <div className="flex max-h-24 flex-col overflow-x-hidden overflow-y-auto rounded border border-neutral-100 text-[11px] text-neutral-500">
      <button
        type="button"
        className="shrink-0 self-end px-1 text-neutral-400 underline decoration-dotted decoration-neutral-300 underline-offset-2"
        onClick={() => setExpanded(false)}
      >
        collapse
      </button>
      {redrafts.map((e, i) => (
        <div
          key={e.seq}
          className="flex items-center gap-2 px-1 py-0.5 odd:bg-neutral-50 hover:bg-neutral-100"
        >
          <span className="w-10 shrink-0 text-neutral-400">{`Lap ${lapNumbers[i]}`}</span>
          <span className="w-10 shrink-0 truncate capitalize">{e.node}</span>
          <IoPreviewCell sessionId={sessionId} seq={e.seq} size={e.input_size} field="input_text" />
          <IoPreviewCell sessionId={sessionId} seq={e.seq} size={e.output_size} field="output_text" />
        </div>
      ))}
    </div>
  );
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
async function ensureSandboxProvisioned(threadId: string, owner: string, repo: string, branch: string): Promise<void> {
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

export function SessionOverview({ owner, repo, branch }: { owner: string; repo: string; branch: string }) {
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
  // Exposed separately from mappedFailureTarget (root-caused 2026-09-13): this is the PRE-mapped
  // value -- often a REBUILD_PLACEMENTS rebuildKey, not a real stage key -- needed by
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
  const boundaryLabel = PIPELINE_STAGE_ORDER.find((s) => s.key === boundaryKey)?.label ?? boundaryKey ?? "This stage";
  // The "redo a different stage instead" disclosure's own candidate list (root-caused 2026-09-13
  // UX redesign): every OTHER stage this run has genuinely approved, excluding the boundary --
  // `stage.status === "approved"` is the same live, trustworthy reachability proof
  // `sessions_api.py`'s rewind-to-stage endpoint itself now accepts (falls back to it whenever the
  // durable current_stage column disagrees). Naturally empty in the durable-only fallback view
  // (state.stages has no live per-stage data yet there) -- nothing to offer alternatives from, so
  // the disclosure correctly renders nothing rather than guessing.
  const otherApprovedStages = isFailedBoundary
    ? PIPELINE_STAGE_ORDER.filter((s) => s.key !== boundaryKey && state.stages?.[s.key]?.status === "approved")
    : [];
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
  async function handleRestart(stageKey: string) {
    const label = PIPELINE_STAGE_ORDER.find((s) => s.key === stageKey)?.label ?? stageKey;
    // Only the boundary's OWN stage qualifies for the cheap retry -- a user who explicitly picked
    // a DIFFERENT (earlier) stage via its own row's button is asking for a real rewind to THAT
    // stage, which still needs the full reset regardless of why the boundary itself failed.
    if (isCannotVerifyBoundary && stageKey === boundaryKey) {
      if (
        !window.confirm(
          "This failed because no sandbox was available to run the check -- nothing about " +
            `${label}'s own work was judged and found wrong. Retrying just re-runs that check ` +
            "against a fresh sandbox; it does not reset or redo any stage. Continue?",
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
      await ensureSandboxProvisioned(threadId, owner, repo, branch);
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
      await ensureSandboxProvisioned(threadId, owner, repo, branch);
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

  // Stuck bespoke-cluster stage (e2e) recovery (Change 5, root-caused 2026-09-12): e2e has no
  // StageState of its own (e2e_nodes.py is a bespoke cluster -- see MetricsBar.tsx's own e2ePill
  // comment), so it never surfaces as a boundary row above -- a session whose last real STAGES
  // entry (through metrics-exit) is already "Approved" gets `firstNonApprovedKey === null` and no
  // restart affordance anywhere, even when e2e itself is genuinely stuck (its own `status` field
  // stays "running" forever on a crash -- e2e_nodes.py only ever transitions it away from
  // "running" on specific explicit branches, never on an unhandled exception). Cross-checked
  // against the run's actual heartbeat (`runActivity.runActive`, the same liveness signal already
  // used one row up for an ordinary stage's "drafting" status) so this never fires during a
  // genuinely healthy e2e run -- only when the persisted status and the real heartbeat disagree.
  //
  // Same bug class as rebuildPhase's own fix, user-reported again 2026-09-13 ("FE must show true
  // live state"): `state.e2e` is never reset between attempts, so a leftover "running" from an
  // EARLIER, since-superseded pass through e2e (this same run had one, 12/12 passed, long before
  // a later rewind sent it all the way back to Remediation) kept firing this banner while the run
  // sat idle at Remediation -- nowhere near e2e in this life. Bounded the same way: e2e is only
  // structurally CURRENT when its own prerequisite (Remediation) has actually approved and its own
  // successor (Adversarial Compliance) hasn't started yet -- exactly rebuildPhase's
  // prior-approved/next-not-started window, inlined here since e2e isn't a RebuildPlacement.
  const e2eCurrentlyRelevant =
    state.stages?.["remediation"]?.status === "approved" &&
    (state.stages?.["adversarial-compliance"]?.status ?? "not_started") === "not_started";
  const e2eStuck = e2eCurrentlyRelevant && state.e2e?.status === "running" && runActivity?.runActive === false && !finishedWithVerdict;
  // "FE must show true live state" (user directive, 2026-09-13): a session-level active run
  // (runActivity.runActive === true, backed by the real heartbeat/container check -- see
  // run_activity.is_active) must never show an action button implying the user needs to do
  // something, even on the frontier row whose OWN draft hasn't started yet -- the run is already
  // progressing on its own (e.g. still finishing an earlier rebuild gate). Explicit `=== true`
  // here, not the `!== false` tri-state pattern `running` uses elsewhere: an unknown/not-yet-loaded
  // signal must still let the button through (never hide a genuinely-needed action on a guess),
  // only a CONFIRMED-active run suppresses it. Component-level (not per-row): used by both
  // rendering branches below, and doesn't depend on which row is being rendered.
  const sessionGenuinelyActive = runActivity?.runActive === true;
  async function handleResumeStuckE2e() {
    if (
      !window.confirm(
        "This run appears to have stalled during automated testing -- nothing is currently " +
          "processing it. Resume to pick back up where it left off?",
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
  }

  // Structural (node_started/node_finished/gate_*) events only -- this tab never needed
  // tool_call/reasoning detail (use-run-events.ts's memory fix, 2026-09-22). Duration/cost numbers
  // come from the server-computed summary below instead of being re-derived from this array.
  const events = useStructuralRunEvents();
  // Server-computed per-stage/per-rebuild-placement duration+cost (Overview-tab fix, 2026-09-22):
  // replaces this tab re-deriving those two numbers from the full held event array on every
  // render, and is what makes a rebuild placement's own duration/cost correct for the first time
  // (see agent/src/run_event_summary.py + agent/src/rebuild.py's retag). Polls only while the run
  // is active.
  const summary = useSessionSummary(threadId, runActivity?.runActive);
  // Lifted out of perStage's own memo below (which used to compute this only for its own local
  // use) so RebuildRow's phase check (further down) can share the exact same fast-channel signal
  // instead of falling back to the slower state-snapshot check alone -- see rebuildPhase's own
  // docstring, "two stages active at once" (root-caused 2026-09-11).
  const runningPhases = useMemo(
    () => computeRunningPhases(events, runActivity?.runActive ?? null),
    [events, runActivity?.runActive],
  );
  // Redraft history + lap count only now (Overview-tab fix, 2026-09-22: duration/cost moved to the
  // server-computed `summary` above, which is where a rebuild placement's numbers now come from
  // too). `RedraftHistoryCell` still needs its own per-stage event list for per-lap detail
  // regardless of where the aggregate numbers live, so this narrower pass stays client-side.
  const perStage = useMemo(() => {
    // Per-stage "most recent run_id touching it" (root-caused 2026-09-13, cost-accuracy fix for
    // the recovery-UX redesign): this used to fold EVERY event ever recorded for a stage,
    // cumulative across every rewind/resume this whole session ever had -- a stage rewound more
    // than once could show 2-3x its real next-redo cost, exactly when a user is deciding whether
    // to redo it. A single session-wide "latest run_id" (computeRunningPhases' own convention,
    // use-run-events.ts) is the wrong fix here: it would blank out the redraft history for a stage
    // that was approved under an OLDER run_id and never touched again by the latest attempt, which
    // is real, still-relevant history, not staleness. Each stage keeps only ITS OWN latest run_id's
    // events instead -- events are oldest-first, so the last write per stage wins.
    const latestRunIdByStage = new Map<string, string>();
    for (const e of events) {
      if (!e.stage) continue;
      latestRunIdByStage.set(e.stage, e.run_id);
    }
    const byStage = new Map<string, { rejections: number; node: string | undefined; redrafts: RunLogEvent[] }>();
    for (const e of events) {
      if (!e.stage || e.run_id !== latestRunIdByStage.get(e.stage)) continue;
      const entry = byStage.get(e.stage) ?? { rejections: 0, node: undefined, redrafts: [] };
      // Redraft-history column (Workstream 3): one row per completed draft/audit/fix call --
      // node_finished only, since that's the point input_size/output_size/payload.cycle are
      // populated (session_key.md's Workstream 2 fix + graph.py's encode_io_text capture).
      // `events` is already oldest-first (use-run-events.ts's own contract), and seq is a durable
      // monotonic IDENTITY, so simply appending in iteration order is already the "accurate
      // ordered" list the redraft-history column needs -- no separate sort.
      if (e.type === "node_finished" && (e.node === "draft" || e.node === "audit" || e.node === "fix")) {
        entry.redrafts.push(e);
      }
      byStage.set(e.stage, entry);
    }
    // See computeRunningPhases' own docstring for why this can't just be `stage.status ===
    // "drafting"`: a non-gated stage's status is stale/misleading between verify attempts.
    // `rejections` (the "Redrafts" count column) used to count ONLY `gate_resolved`/"rejected"
    // events -- a real signal, but one that exists for just the 3 HUMAN-gated stages (tech-stack,
    // specification, plan). Every other stage (ac-to-tests, minimal-code-to-green, remediation,
    // ...) redrafts from a DETERMINISTIC verify failure instead, a different event type never
    // counted here -- so a stage that visibly ran 4 laps (Redraft History, right next to this
    // column) showed a blank "Redrafts" cell, which reads as a bug even though the narrower
    // definition was technically doing what it said. Derived from `redrafts` instead, now that
    // it's fully populated for every stage: same "count a new lap on each draft node" rule
    // RedraftHistoryCell already uses (see its own docstring), so this column and the one next to
    // it can never visually disagree. Lap 1 is the first attempt, not a redraft -- floors at 0.
    for (const [stageKey, entry] of byStage) {
      entry.node = runningPhases.get(stageKey);
      const laps = entry.redrafts.filter((e) => e.node === "draft").length;
      entry.rejections = Math.max(0, laps - 1);
    }
    return byStage;
  }, [events, runningPhases]);

  // What a rewind to `stageKey` actually resets (root-caused 2026-09-13 UX redesign): mirrors
  // agent/src/graph.py:2515-2542's own rewind-to-stage handling exactly -- every real stage from
  // `stageKey` onward, plus every REBUILD_PLACEMENTS entry whose `afterStageKey` sits at or after
  // it (graph.py:2536-2538) -- so the enumerated label list and cost match the real consequence,
  // not an approximation. `state.stages?.[s.key] == null` filters out PIPELINE_STAGE_ORDER's own
  // legacy/never-populated tail keys (brownfield-baseline, raw-requirements, and four retired
  // stage keys the current graph never writes -- see that file's own comment) so the confirm copy
  // never lists a phantom stage. `summary` (server-computed, Overview-tab fix 2026-09-22) is
  // keyed by the same normalized stage/placement key either way, so each stage's own trailing
  // placement (if any) must still be looked up via REBUILD_PLACEMENTS explicitly -- a direct
  // `summary.get(s.key)` for the placement itself would silently always miss.
  function stagesResetByRewind(stageKey: string): { labels: string[]; approxCost: number } {
    const startIdx = stageOrderIndex(stageKey);
    const labels: string[] = [];
    let approxCost = 0;
    for (const s of PIPELINE_STAGE_ORDER) {
      if (stageOrderIndex(s.key) < startIdx) continue;
      if (state.stages?.[s.key] == null) continue;
      labels.push(s.label);
      approxCost += summary.get(s.key)?.cost ?? 0;
      const placement = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === s.key);
      if (placement) approxCost += summary.get(placement.rebuildKey)?.cost ?? 0;
    }
    return { labels, approxCost };
  }

  return (
    <ViewContainer>
      {/* No local cost chip here: MetricsBar (AppShell) already mounts its own LiveCostChip as
          soon as there's any live cost to show -- a second one here was a plain duplicate, not a
          fallback for an actually-uncovered case (root-caused 2026-09-11, user-reported dupe). */}
      <h1 className="text-lg font-semibold">Session Overview</h1>

      {/* Change 5 (root-caused 2026-09-12): e2e is a bespoke cluster with no StageState/boundary
          row of its own, so a session genuinely stuck inside it -- container alive, nothing
          driving it -- otherwise gets NO recovery affordance anywhere on this tab once every real
          STAGES entry already shows "Approved". Standalone, not nested in a stage row, since e2e
          has no row to nest it in. */}
      {e2eStuck && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          <p>
            This run appears to have stalled during automated testing — nothing is currently
            processing it.
          </p>
          <button
            type="button"
            className="shrink-0 rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
            disabled={restarting}
            onClick={() => void handleResumeStuckE2e()}
          >
            {restarting ? "Working…" : "Resume this run"}
          </button>
        </div>
      )}

      {/* Consolidated recovery panel (root-caused 2026-09-13, user-reported: once a real failure
          existed, every approved stage grew its own black "Restart workflow from this stage"
          button -- five near-identical CTAs with no indication some were free retries and others
          were expensive full redos, and no explanation that an already-approved downstream stage
          gets SKIPPED, not redone (should_skip_draft, agent/src/graph.py:4433-4453), when the
          graph reaches it again. Replaces every per-row raw-failure-text + button block below (in
          BOTH the live and durable-fallback rendering branches) with exactly one panel, rendered
          once here -- above both branches, since isFailedBoundary is computed once, above either
          of them. Pivot (root-caused 2026-09-12): the old top banner here read live
          `state.run_failure` and fired a bare, untargeted `runAgent()` -- no confirm, no stage
          targeting, no rebuild-sub-state reset. This panel is that pivot's proper replacement:
          the one explicit, stage-anchored action, now explained instead of just targeted. */}
      {isFailedBoundary && mappedFailureTarget && (
        <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-900">
          <p className="font-medium">
            {describeFailure(rawFailureStage, failureTypeText, failureMessageText, boundaryLabel)}
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
              {isCannotVerifyBoundary
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
              {restarting ? "Working…" : isCannotVerifyBoundary ? "Retry this check" : `Redo ${boundaryLabel}`}
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
                        onClick={() => void handleRestart(s.key)}
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
      )}

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
            <span>Redraft History</span>
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
                    {/* No live event data in this durable-only fallback view -- nothing to show. */}
                    <span />
                  </div>
                  {/* The failure case is fully handled by the consolidated recovery panel above
                      now -- this row only ever needs the plain "nothing failed, just continue"
                      affordance, same shared component the live branch below uses. */}
                  {isBoundary && !finishedWithVerdict && !isFailedBoundary && !running && !sessionGenuinelyActive && !e2eStuck && (
                    <ContinueAction restarting={restarting} onClick={() => void handleRestart(s.key)} />
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
            <span>Redraft History</span>
          </div>
          <ol className="flex flex-col gap-2">
            {stages.map(([key, stage]) => {
              const timing = perStage.get(key);
              // Server-computed duration/cost (Overview-tab fix, 2026-09-22) -- see `summary`'s
              // own declaration above. `timing` (perStage) still owns redraft history/count and
              // the live "currently drafting/auditing" node label; `s` owns duration/cost only.
              const s = summary.get(key);
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
              // Pivot (root-caused 2026-09-12): the restart/continue action on whichever row is
              // the durable boundary -- computed once above from durable data, so it lands on
              // the correct row (the mapped real stage, e.g. "ac-to-tests" for an
              // r_ac_to_tests rebuild failure) even with live per-stage state also available.
              // Never shown while genuinely still running -- nothing to continue then.
              const isBoundaryRow = key === boundaryKey;
              // The failure case (both the boundary row's own action and every other approved
              // stage's "redo from here" option) is fully handled by the consolidated recovery
              // panel above now (root-caused 2026-09-13 UX redesign) -- this row only ever needs
              // the plain "nothing failed, just continue" affordance, on its own frontier row,
              // same shared component the durable-fallback branch above uses.
              const showPlainContinue =
                !finishedWithVerdict && !sessionGenuinelyActive && !e2eStuck && isBoundaryRow && !isFailedBoundary && !running;
              // At most one placement follows any given real stage today (REBUILD_PLACEMENTS has
              // no two entries sharing an afterStageKey) -- find(), not filter().
              const placement = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === key);
              const phase = placement && rebuildPhase(state, placement, runActivity?.runActive, runningPhases);
              const rebuildRow = placement && phase && (
                <RebuildRow
                  key={placement.rebuildKey}
                  placement={placement}
                  phase={phase}
                  timing={summary.get(placement.rebuildKey)}
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
                      {s && s.last > s.first ? formatDuration(s.last - s.first) : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {s?.costKnown ? `$${s.cost.toFixed(2)}` : ""}
                    </span>
                    <span className="text-right text-xs text-neutral-500">
                      {timing && timing.rejections > 0 ? `${timing.rejections}×` : ""}
                    </span>
                    <span
                      className={`flex items-center justify-end gap-1.5 ${failedHere ? "text-red-700" : "text-neutral-500"}`}
                    >
                      <StatusDot
                        tone={
                          failedHere
                            ? "red"
                            : running
                              ? "amber"
                              : stage.status === "approved"
                                ? "green"
                                : stage.status === "needs_clarification" || stage.status === "ready_for_review"
                                  ? "amber"
                                  : "gray"
                        }
                      />
                      {running && <RunningSpinner />}
                      {failedHere
                        ? "Failed"
                        : running
                          ? (timing?.node ? (NODE_PHASE_LABEL[timing.node] ?? "Running") : "Drafting")
                          : (STATUS_LABEL[stage.status] ?? stage.status)}
                    </span>
                    <RedraftHistoryCell sessionId={threadId} redrafts={timing?.redrafts ?? []} />
                  </div>
                  {note && <p className="mt-1 text-xs text-neutral-500">{note}</p>}
                  {!note && stage.audit_findings?.length > 0 && <AuditFindingsNote findings={stage.audit_findings} />}
                  {showPlainContinue && <ContinueAction restarting={restarting} onClick={() => void handleRestart(key)} />}
                  {key === "metrics-exit" && finishedWithVerdict && runActivity?.mergeReady === false && (
                    <div className="mt-2 flex items-start justify-between gap-3 border-t border-neutral-100 pt-2">
                      <p className="text-xs text-neutral-500">
                        {"This run finished but is not ready to merge -- see the Report tab for why. " +
                          "Ask the agent to fix it, or re-verify if you've already fixed it yourself."}
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
