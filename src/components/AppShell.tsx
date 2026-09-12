"use client";

import {
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
  useInterrupt,
} from "@copilotkit/react-core/v2";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AgentNarrationDrawer } from "@/components/AgentNarrationDrawer";
import { BuildView } from "@/components/BuildView";
import { ContainerStatusButton } from "@/components/ContainerStatus";
import { LiveCostChip } from "@/components/LiveCostChip";
import { MetricsBar, type MetricThresholds } from "@/components/MetricsBar";
import { PlanView } from "@/components/PlanView";
import { QualityView } from "@/components/QualityView";
import { ReportView, type FilesChangedSummary, type ReportExtras } from "@/components/ReportView";
import { RequirementsView } from "@/components/RequirementsView";
import { SessionOverview } from "@/components/SessionOverview";
import { SpecificationView } from "@/components/SpecificationView";
import { TechStackView } from "@/components/TechStackView";
import { RunningSpinner, Spinner } from "@/components/Spinner";
import { terminateSession } from "@/lib/agent-client";
import { InterruptProvider, useOpenInterrupt } from "@/lib/interrupt-context";
import { rawProxyUrl } from "@/lib/raw-proxy";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useRunActivity } from "@/lib/run-activity-context";
import { computeRunningStages, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import {
  buildStarted,
  type EscalationPayload,
  type MergeReadinessReport,
  PIPELINE_STAGE_ORDER,
  stageOrderIndex,
  type StageState,
  TAB_STAGE_GROUPS,
  type StageKey,
  type WorkflowState,
} from "@/lib/workflow-types";

type ViewId = "tech-stack" | "requirements" | "specification" | "plan" | "build" | "quality" | "report" | "overview";
type DotState = "running" | "done" | "error" | "awaiting";

const DOT_CLASS: Record<DotState, string> = {
  running: "bg-blue-500 animate-pulse",
  awaiting: "bg-amber-500 animate-pulse",
  done: "bg-emerald-500",
  error: "bg-red-500",
};

/** Dot for a tab whose status derives from ordinary StageStates (TAB_STAGE_GROUPS). Green dots
 * intentionally clear on resubmission: intake resets later stages to not_started on each fresh
 * run, and the dots simply reflect that.
 *
 * `runningStages` (computeRunningStages, use-run-events.ts) backstops `status === "drafting"`:
 * a non-gated stage (ac-to-tests, minimal-code-to-green, ...) cycles through "ready_for_review"
 * between verify attempts -- a generic status name the backend reuses for "draft phase done"
 * regardless of whether a human is involved -- so relying on `status` alone showed a stage that
 * was actively retrying as "awaiting" almost the entire time (user feedback 2026-09-01). Checked
 * FIRST: the live event stream is more current than state, which only pushes on a gate pause. */
function stageGroupDot(state: WorkflowState, keys: StageKey[], runningStages: Set<string>): DotState | undefined {
  // Checked before the stages.length guard below: mid-run reattach (user feedback 2026-09-01)
  // means `state.stages` can be completely empty for a while even though the run is genuinely
  // active -- the event stream still knows, so this must not wait on stage state existing at all.
  if (keys.some((k) => runningStages.has(k))) return "running";
  const stages = keys.map((k) => state.stages?.[k]).filter((s) => s != null);
  if (stages.length === 0) return undefined;
  if (stages.some((s) => s.status === "drafting")) return "running";
  if (stages.some((s) => s.status === "ready_for_review" || s.status === "needs_clarification")) return "awaiting";
  if (stages.some((s) => s.last_verification && !s.last_verification.passed && s.status !== "approved")) return "error";
  if (stages.every((s) => s.status === "approved")) return "done";
  return undefined;
}

/** Reverse of TAB_STAGE_GROUPS: which tab a durable `current_stage` value belongs to. Returns
 * undefined for a stage TAB_STAGE_GROUPS doesn't cover (quality/report's own stages aren't
 * listed there either) -- callers must treat that as "nothing to correct", not an error. */
function tabForStage(stageKey: string): ViewId | undefined {
  const found = Object.entries(TAB_STAGE_GROUPS).find(([, keys]) => (keys as string[]).includes(stageKey));
  return found?.[0] as ViewId | undefined;
}

export function AppShell({
  owner,
  repo,
  workBranch,
  metricThresholds,
  resume,
  filesChanged,
  reportExtras,
}: {
  /** Repo coordinates for the Report tab's raw-content proxy URLs (screenshots) -- not needed by
   * anything else here, since every other view scopes itself through useWorkflowThread's
   * threadId instead. */
  owner: string;
  repo: string;
  /** This session's own work_branch (agent/src/branch_naming.py) -- the git ref screenshots
   * actually live on, resolved once server-side (the workflow page) since it's the same value
   * useWorkflowThread's threadId already identifies this session by. */
  workBranch: string;
  metricThresholds: MetricThresholds;
  /** ?resume=1 from the workflow page -- fires the run once the sandbox is ready even on a
   * thread that already has state, since the ordinary auto-trigger below is deliberately
   * suppressed in that case (ordinary reloads must not re-run automatically; a Resume click
   * should). */
  resume?: boolean;
  /** The git diff-stat/commit-log block for the Report tab, resolved server-side (workflow page)
   * from this run's committed report.json -- the one piece of a completed session's exit report
   * that lives only in that committed artifact, never in live LangGraph state. Undefined for an
   * in-progress session (nothing committed yet); ReportView already renders nothing for that. */
  filesChanged?: FilesChangedSummary | null;
  /** Findings/AC-execution/stage-summary detail from the same committed report.json, resolved
   * server-side alongside filesChanged -- same completed-session-only availability. */
  reportExtras?: ReportExtras | null;
}) {
  const { threadId, runtimeAgentId, localAgentId } = useWorkflowThread();
  const { agent, isReady: agentIsReady } = useAgent({
    agentId: localAgentId,
    runtimeAgentId,
    threadId,
    updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged],
  });
  const [activeView, setActiveView] = useState<ViewId>("tech-stack");
  const { copilotkit } = useCopilotKit();
  const [sandboxStatus, setSandboxStatus] = useSandboxStatus();
  // Declared early (not down by the poll that populates it) so runningStages below can read it --
  // `null` until that poll's first response arrives; see computeRunningStages' own tri-state note.
  const [runActivity, setRunActivity] = useRunActivity();
  const router = useRouter();
  const [stoppingContainer, setStoppingContainer] = useState(false);
  // Agent Narration Drawer: plain local state, no new context -- AppShell already persists across
  // tab switches (views are hidden, not unmounted), so this survives tab changes for free. Manual
  // toggle only (no auto-open on a new turn starting): the pipeline advances through many turns
  // without direct user action per turn, and auto-popping this over whatever tab someone's working
  // in every time a turn starts would fight their navigation constantly.
  const [narrationOpen, setNarrationOpen] = useState(false);

  const state = (agent.state ?? {}) as WorkflowState;
  const specification = state.stages?.specification;
  const plan = state.stages?.plan;
  const runEvents = useRunEvents();
  const runningStages = useMemo(
    () => computeRunningStages(runEvents, runActivity?.runActive ?? null),
    [runEvents, runActivity?.runActive],
  );
  // Always-fresh handle for effects below whose own deps intentionally exclude `state` (recreating
  // a poll's setInterval on every state tick would be wasteful) but still need this render's value.
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = (agent.state ?? {}) as WorkflowState;
  }, [agent.state]);

  // Focus follows the pipeline (user ask 2026-08-31): when a stage starts needing the user (a
  // gate opens) or a new phase begins, switch to its tab instead of making the user chase the
  // amber dot. Two modes:
  //  - TRANSITION: a stage's status changed this mount -> jump to the mapped tab.
  //  - FIRST LOAD (no previous statuses seen): land on the most relevant tab for the state as
  //    hydrated -- a returning user opens where the action is, not on the Tech Stack default.
  // Manual clicks always win afterwards: auto-switches only ever fire on fresh transitions.
  const stagesForFocus = state.stages;
  const prevStageStatusRef = useRef<Record<string, string> | null>(null);
  useEffect(() => {
    if (stagesForFocus == null || Object.keys(stagesForFocus).length === 0) return;
    const stages = stagesForFocus as Record<string, { status?: string } | undefined>;
    const status = (key: string) => stages[key]?.status;
    const prev = prevStageStatusRef.current;
    const current: Record<string, string> = {};
    for (const [key, stage] of Object.entries(stages)) if (stage?.status) current[key] = stage.status;
    prevStageStatusRef.current = current;

    // Ordered latest-phase-first: the furthest stage that newly needs attention wins.
    const RULES: { key: string; at: string; to: ViewId; also?: () => boolean }[] = [
      { key: "metrics-exit", at: "approved", to: "report" },
      { key: "exit", at: "approved", to: "report" },
      { key: "ac-to-tests", at: "drafting", to: "build" },
      { key: "plan", at: "ready_for_review", to: "plan" },
      { key: "specification", at: "ready_for_review", to: "specification" },
      // Tech stack confirmed -> the only next action is typing requirements. Skip for
      // resumed/delta threads that already carry them -- checked by STATUS, not key presence:
      // intake pre-creates every stage entry at not_started, so `== null` never fired (observed
      // live 2026-08-31, the jump silently skipped).
      {
        key: "tech-stack",
        at: "approved",
        to: "requirements",
        also: () => (stages["raw-requirements"]?.status ?? "not_started") === "not_started",
      },
    ];
    // setState-in-effect is the point here: activeView reacts to SERVER stage transitions (an
    // external store), not to derivable render-time data -- same exemption shape as the
    // one-time seeds in RequirementsView.
    if (prev == null) {
      const landing = RULES.find((r) => status(r.key) === r.at && (r.also?.() ?? true));
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (landing) setActiveView(landing.to);
      return;
    }
    const fired = RULES.find((r) => status(r.key) === r.at && prev[r.key] !== r.at && (r.also?.() ?? true));

    if (fired) setActiveView(fired.to);
  }, [stagesForFocus]);

  // Build tab wins the landing race against a same-tick stale gate (e.g. a requirements-delta
  // reopening Plan's "ready_for_review" while minimal-code-to-green is genuinely running): RULES
  // above only catches ac-to-tests's "drafting" for the Build jump, but a non-gated build stage
  // spends most of its active time in "ready_for_review" between verify attempts, not "drafting"
  // (same status-cycling flaw stageGroupDot backstops with runningStages for the tab dots, above).
  // Fires only on the false->true edge so it never fights a manual tab click made while build
  // keeps running (found live 2026-09-01: landed on Plan while Build was active).
  const wasBuildRunningRef = useRef(false);
  useEffect(() => {
    const buildRunning = TAB_STAGE_GROUPS.build.some((k) => runningStages.has(k));
    if (buildRunning && !wasBuildRunningRef.current) setActiveView("build");
    wasBuildRunningRef.current = buildRunning;
  }, [runningStages]);

  // Third, narrower backstop (Workflow Liveness Fix; user-reported: landed on Tech Stack with a
  // session already at ac-to-tests): the two mechanisms above already cover most of this --
  // RULES' own first-load branch below reacts once `state.stages` hydrates, and the effect just
  // above reacts to the live event stream -- but a non-gated build stage spends most of its real
  // running time at persisted status "ready_for_review" (RULES misses it) between verify attempts
  // rather than "drafting", and the event stream can lag on first paint. The durable row's plain
  // `current_stage` string is the cheapest, fastest-arriving signal (no dependency on state.stages
  // or the events poll) -- fires once, only while still sitting on the initial default, so it
  // never fights a tab the user already clicked.
  const durableTabLandedRef = useRef(false);
  useEffect(() => {
    if (durableTabLandedRef.current) return;
    if (runActivity?.currentStage == null) return;
    durableTabLandedRef.current = true;
    if (activeView !== "tech-stack") return; // already navigated (manually or by a sibling effect)
    const tab = tabForStage(runActivity.currentStage);
    // setState-in-effect is the point here, same exemption as the RULES effect above: reacting to
    // a durable SERVER value (current_stage), not to derivable render-time data.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (tab && tab !== "tech-stack") setActiveView(tab);
    // activeView intentionally excluded -- read once at fire time (one-shot, ref-guarded), not a
    // reactive dependency; listing it would re-run this effect on every later tab switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runActivity?.currentStage]);

  // Container-pill liveness poll (found live 2026-08-31: the pill said "Connected" while a
  // restarted agent had NO sandbox registered -- SandboxSessionBoot sets "ready" once after the
  // provision POST and nothing ever re-checked, so an agent restart or a dead container left the
  // pill green until the next run failed). The agent side is real-time (a docker-events watcher
  // evicts a dead container's registry entry within milliseconds, and reads verify against
  // `docker inspect`); GET /api/sessions/{id} surfaces that as `container_alive`, and this poll
  // is just the delivery hop -- every 10s and on window focus, so the pill lags the daemon by at
  // most one tick. Never interferes with an in-flight provision ("provisioning" is skipped);
  // recovers to "ready" on its own if the sandbox comes back.
  const sandboxStatusRef = useRef(sandboxStatus);
  useEffect(() => {
    sandboxStatusRef.current = sandboxStatus;
  }, [sandboxStatus]);
  // Durable session row (dbo.sessions, via the same poll) -- current_stage/status survive an agent
  // restart and a client reload alike, unlike the live AG-UI state stream below. Used only to
  // detect the mid-run reattach gap (see isReattaching below); never a substitute for `state`.
  const [durableRow, setDurableRow] = useState<{
    current_stage: string | null;
    status: string;
    awaiting_gate: boolean | null;
    container_alive: boolean;
  } | null>(null);
  // One-shot, separate from the fresh-session auto-trigger's own ref below: that effect fires (or
  // doesn't) once at mount and never retries, so a reattach whose gate wasn't open YET at mount
  // never got a second chance -- found live 2026-08-31, right after Plan's gate genuinely opened,
  // on a tab that had been sitting on the "Reconnecting…" banner since before that: the banner
  // does not clear on its own, contradicting its own copy ("this page updates automatically").
  const reattachTriggeredRef = useRef(false);
  // Exposes the durable-row stream's own reconnect-if-closed check to the Resume/Reattach button
  // below (root-caused 2026-09-11): that stream's "done" handler closes it for good on a REAL
  // terminal status, with no reopen logic of its own -- by design, since most "done"s (completed)
  // truly are final. But "failed" is also terminal-shaped here, and failed sessions CAN be
  // resumed (this file's own canReattach/Resume button exists for exactly that) -- and once
  // resumed, the OLD closed EventSource never reopens itself. The only existing recovery was
  // indirect (onFocus, below) and only fires on an actual focus transition, which a tab the user
  // never alt-tabs away from may not see for a long time -- observed live, thread 8242ea6d: the
  // reconnect banner and a Build stage card both kept showing the stage as of the LAST update
  // before that "done" fired (minimal-code-to-green), well after the resumed run had genuinely
  // moved on to remediation. Calling this at the exact moment the user clicks Resume/Reattach is a
  // direct, immediate fix instead of waiting on an indirect signal to eventually catch up.
  const reconnectDurableRowIfClosedRef = useRef<() => void>(() => {});
  // SSE tail of the durable session row, replacing what used to be a 10s `setInterval` fetch loop
  // (both here: `/api/sessions/{id}/stream`, backed by the agent's stream_session_row -- see that
  // function's own docstring). Pushes updates within a few seconds instead of up to 10s late, and
  // -- the actual bug this was built to fix -- reflects a run_headless.py-driven session's
  // run_active/interrupted correctly, since stream_session_row recomputes those from
  // run_activity.heartbeat's cross-process signal every tick, not this browser tab's own
  // in-memory state.
  useEffect(() => {
    function applyRow(row: {
      container_alive?: boolean;
      current_stage: string | null;
      status: string;
      awaiting_gate: boolean | null;
      run_active?: boolean;
      interrupted?: boolean;
      finished_with_verdict?: boolean;
      failure_stage?: string | null;
      failure_type?: string | null;
      failure_message?: string | null;
      merge_ready?: boolean | null;
    }) {
      // A terminal session (completed/failed/rejected) has no container to be alive in the first
      // place -- SandboxSessionBoot's `skip` never even asked for one. Calling that
      // "error"/Disconnected here would overwrite its correct "terminated" a few seconds after
      // load with a status implying something failed, when nothing did. Still "ready"/"error" as
      // before for an in_progress session (the one case a live container is actually expected).
      // Scoped to JUST this line (root-caused 2026-09-11): this used to guard the whole poll tick
      // back when this was a setInterval REST poll (skipping one tick was harmless -- the next
      // tick a few seconds later just tried again). SSE only re-sends a row when it CHANGES
      // (sessions_api.py's stream_session_row dedupes against last_payload), so the old
      // whole-handler early return silently dropped durableRow/runActivity for good whenever the
      // first SSE row raced ahead of SandboxSessionBoot's own provision POST resolving --
      // observed live: sandboxStatus later reached "ready" via that separate path, but
      // runActivity.currentStage stayed null forever, so every durable-fallback tab (Tech Stack
      // included) rendered as if the session had never run.
      if (sandboxStatusRef.current !== "provisioning") {
        setSandboxStatus(row.container_alive ? "ready" : row.status === "in_progress" ? "error" : "terminated");
      }
      setDurableRow({
        current_stage: row.current_stage, status: row.status, awaiting_gate: row.awaiting_gate,
        container_alive: row.container_alive ?? false,
      });
      // Same payload, lifted into context so BuildView/SessionOverview/SpecificationView/
      // PlanView/RequirementsView can read run_active/interrupted without a second fetch.
      setRunActivity({
        runActive: row.run_active ?? false,
        interrupted: row.interrupted ?? false,
        awaitingGate: row.awaiting_gate,
        currentStage: row.current_stage,
        status: row.status,
        finishedWithVerdict: row.finished_with_verdict ?? false,
        containerAlive: row.container_alive ?? false,
        failureStage: row.failure_stage ?? null,
        failureType: row.failure_type ?? null,
        failureMessage: row.failure_message ?? null,
        mergeReady: row.merge_ready ?? null,
      });
      // The moment the durable row reports the run PAUSED at its own gate, a blank run request
      // hits ag_ui_langgraph's pending-interrupt short-circuit and main.py's
      // _ReattachStateAgent injects a full STATE_SNAPSHOT into it -- exactly the mechanism a
      // manual reload was relying on. Firing it here means this tab recovers on its own, no
      // reload needed. Guarded so it only ever fires once per mount; a stages-non-empty client
      // (the ordinary case) never reaches this branch at all.
      if (
        row.awaiting_gate &&
        Object.keys(stateRef.current.stages ?? {}).length === 0 &&
        !reattachTriggeredRef.current
      ) {
        reattachTriggeredRef.current = true;
        void copilotkit.runAgent({ agent });
      }
    }

    let source: EventSource | null = null;
    function open() {
      source = new EventSource(`/api/sessions/${encodeURIComponent(threadId)}/stream`);
      source.addEventListener("session", (ev) => {
        try {
          applyRow(JSON.parse((ev as MessageEvent).data));
        } catch {
          // Malformed payload -- ignore; the next tick/reconnect carries the real data.
        }
      });
      source.addEventListener("not_found", () => {
        setSandboxStatus("terminated"); // session deleted elsewhere
        source?.close();
      });
      // 'done' (real terminal status) closes for good; 'reconnect' (the agent's own
      // _SSE_MAX_CONNECTION_SECONDS cap -- see stream_session_row's own comment) means the session
      // can still be in_progress, so it opens a fresh connection instead of going quiet.
      source.addEventListener("done", () => source?.close());
      source.addEventListener("reconnect", () => {
        source?.close();
        open();
      });
    }
    open();

    // Some browsers close an idle EventSource under background/power-saving throttling -- this is
    // the recovery path if that happened while the tab wasn't visible. A healthy open connection
    // needs no action; only force a reconnect if it's actually dead. Shared with the Resume/
    // Reattach button's onClick via the ref below -- same "reopen only if actually closed" check,
    // two different triggers for when it's worth running.
    const reconnectIfClosed = () => {
      if (source && source.readyState === EventSource.CLOSED) open();
    };
    reconnectDurableRowIfClosedRef.current = reconnectIfClosed;
    window.addEventListener("focus", reconnectIfClosed);
    return () => {
      source?.close();
      window.removeEventListener("focus", reconnectIfClosed);
      reconnectDurableRowIfClosedRef.current = () => {};
    };
  }, [threadId, setSandboxStatus, setRunActivity, agent, copilotkit]);

  // Idle-session hydration (root-caused 2026-09-12, user-reported: every tab past Tech Stack
  // rendered as if the session had never run). `agent.state` only ever gets populated by an actual
  // AG-UI run -- a live turn, or the awaiting_gate blank-runAgent trick above, which only covers the
  // three human-gated stages. Post-pivot, nothing auto-fires a run anymore, so an idle session
  // (finished, failed, or simply not yet continued) gets neither, forever -- the durable row above
  // only ever carries current_stage/status, never the actual draft/approved content each tab
  // renders. One-shot, read-only fetch of the LangGraph checkpoint (agent/src/sessions_api.py's
  // get_checkpoint_state -- a pure state read, no node executes, no runAgent call here at all) to
  // hydrate agent.state directly via its own setState. Re-checks stages is still empty right before
  // applying: a real run (a Continue/Restart click, or another tab) may have delivered genuine live
  // state while this fetch was in flight, and that must win, never this stale disk read.
  const checkpointHydratedRef = useRef(false);
  useEffect(() => {
    if (checkpointHydratedRef.current) return;
    // Root-caused 2026-09-12: useAgent (react-core/v2) returns a throwaway PROVISIONAL agent
    // object until its own registration effect resolves the real proxy registered under
    // `localAgentId` -- calling agent.setState() on that provisional instance before `isReady`
    // silently succeeds (it's a real object with a real setState) but reaches no one else, since
    // every OTHER view's own `useAgent({agentId: localAgentId})` call resolves through
    // `copilotkit.getAgent(localAgentId)`, which only starts returning the real registered proxy
    // once this call's own registration effect has completed. Gating on `isReady` (not just
    // `agent`'s own identity change) is what makes this fetch actually reach every tab instead of
    // silently updating a throwaway instance nobody else reads.
    if (!agentIsReady) return;
    checkpointHydratedRef.current = true;
    (async () => {
      try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(threadId)}/checkpoint-state`);
        if (!res.ok) return;
        const snapshot = (await res.json()) as Record<string, unknown>;
        if (Object.keys(stateRef.current.stages ?? {}).length > 0) return;
        if (Object.keys(snapshot).length > 0) agent.setState(snapshot);
      } catch {
        // Transient network failure -- the durable-row fallbacks above already cover this tab in
        // the meantime; nothing else retries this specific fetch, same one-shot contract as every
        // other hydration path in this file.
      }
    })();
  }, [threadId, agent, agentIsReady]);

  // Mid-run reattach gap (backlog item 4; user found confusing live 2026-08-31): a client that
  // (re)connects while the graph is actively drafting/auditing -- no gate open, nothing to pause
  // on -- gets no state snapshot until the run next pauses; today's architecture only delivers one
  // at a gate interrupt. Meanwhile this component's local `activeView` still defaults to its
  // initial "tech-stack", so the user saw the Tech Stack tab's own "Detecting your tech stack…"
  // copy on a session that was actually several stages further along -- read as the app having
  // lost its place. The durable session row (dbo.sessions, unaffected by the gap) is the signal
  // that this is a stale reattach, not a genuine fresh start: `current_stage` past "tech-stack"
  // with the run still `in_progress` while the live stream has delivered nothing at all.
  // Workflow Liveness Fix: must NOT fire once we positively know the run has stopped -- otherwise
  // this banner's own "pipeline keeps running in the background" copy is a lie next to the
  // Interrupted banner's "this run appears to have stopped" a few pixels below it. `interrupted`
  // is a definitive signal (server-computed from run_active + awaiting_gate); `runActivity == null`
  // (not yet loaded) still allows this branch, same tri-state caution as computeRunningStages.
  const isReattaching =
    Object.keys(state.stages ?? {}).length === 0 &&
    durableRow?.status === "in_progress" &&
    durableRow.current_stage != null &&
    durableRow.current_stage !== "tech-stack" &&
    !runActivity?.interrupted;

  // Workflow Liveness Fix false positive (found live 2026-09-06): graph.py's
  // _route_after_repo_scan_baseline deliberately ends the run at END -- not a gate, no
  // interrupt() -- once tech-stack is approved but no requirements have been typed yet ("waits
  // for the Requirements tab", per that router's own docstring). That leaves status=in_progress,
  // run_active=false, awaiting_gate=false: textbook `interrupted` by sessions_api.py's
  // definition, even though nothing crashed -- every fresh session sits in exactly this state
  // right after approving its tech stack. The RULES table above (tech-stack/approved/also:
  // raw-requirements still not_started) already trusts this same pair of stage statuses to mean
  // "waiting on the human to type requirements, not broken" for the auto-navigate jump; reused
  // here to silence the same false alarm for the banner below.
  const isAwaitingFirstRequirements =
    state.stages?.["tech-stack"]?.status === "approved" &&
    (state.stages?.["raw-requirements"]?.status ?? "not_started") === "not_started";

  // Reattach vs Resume (root-caused 2026-09-11, same distinction as SessionHistory.tsx's own
  // fix): a "failed" run always needs a real restart-from-checkpoint regardless of container
  // state (its approval/counters were already revoked by make_escalate_node). An "interrupted"
  // run (nothing attached, but not failed) only needs that if its sandbox is actually gone --
  // container_alive is verified Docker truth (sessions_api._verified_container_alive), not the
  // stale "is a stream attached in THIS process" signal `interrupted` itself is. When the
  // container is alive, copilotkit.runAgent() alone reattaches for free. When it is not,
  // runAgent() alone has nothing to exec into -- only a fresh mount (SandboxSessionBoot) actually
  // reprovisions, so that case reloads the page instead.
  const runFailed = durableRow?.status === "failed";
  const canReattach = !runFailed && Boolean(durableRow?.container_alive);

  // Requirement (root-caused 2026-09-12, non-negotiable): once a stage has ever executed, its tab
  // must never become disabled again, regardless of the run's CURRENT status. Every durable-truth
  // fallback in this file used to gate on `isReattaching`, which itself requires
  // `durableRow?.status === "in_progress"` -- so every one of them went dead the instant status
  // left "in_progress" (failed/completed), which is exactly what silently re-disabled every tab
  // on this exact session after it stopped. `durableRow.current_stage` is a monotonic fact (only
  // ever advances, on a stage's own approval) that stays true forever regardless of what the run
  // is doing right now -- unlike isReattaching, which intentionally resets once status leaves
  // in_progress and must keep doing so for its own (unrelated) "Reconnecting…" banner.
  function durableStageAtLeast(target: string): boolean {
    return stageOrderIndex(durableRow?.current_stage) >= stageOrderIndex(target);
  }

  // Requirement (root-caused 2026-09-12): "Resume picks up from the last checkpoint" named no
  // actual checkpoint -- a user had no way to tell what that even meant. Same lookup the reattach
  // banner just below already uses.
  const failedStageLabel = durableRow?.current_stage
    ? (PIPELINE_STAGE_ORDER.find((s) => s.key === durableRow.current_stage)?.label ?? durableRow.current_stage)
    : null;

  // Pivot (root-caused 2026-09-12, user requirement): the graph must NEVER advance except via one
  // explicit, visible action (SessionOverview's stage-anchored restart/continue button). This
  // effect used to ALSO auto-fire on `?resume=1` and on "the run looks genuinely still running" --
  // both removed. `?resume=1` still exists and is still consumed (by SandboxSessionBoot's `skip`
  // computation, to decide whether to attempt reprovisioning a dead container) and is still
  // stripped from the URL once read here, so a stale link/history entry can't imply meaning it no
  // longer has -- it just no longer ALSO fires the workflow. The only remaining auto-fire case is
  // a thread that has never run at all: `current_stage` is null until a stage's first draft even
  // starts (graph.py's make_draft_node writes it right before the LLM call, not just on
  // approval), so this is the ordinary first kickoff of a brand-new session, not a "resume" in any
  // sense this pivot is about.
  const autoTriggeredRef = useRef(false);
  useEffect(() => {
    if (autoTriggeredRef.current) return;
    if (sandboxStatus !== "ready") return;
    if (resume) {
      router.replace(window.location.pathname, { scroll: false });
      return;
    }
    if (durableRow == null) return; // wait for durable truth, not a guess
    const neverRunBefore = durableRow.status === "in_progress" && durableRow.current_stage == null;
    if (!neverRunBefore) return;
    autoTriggeredRef.current = true;
    void copilotkit.runAgent({ agent });
  }, [sandboxStatus, agent, copilotkit, resume, durableRow, router]);

  // Section 8: the interrupt UI must be reachable regardless of which view is open. Task 7 dropped
  // the CopilotSidebar that renderInChat's default (true) used to publish into; renderInChat:
  // false below gets the rendered element back directly instead, so this component can mount it
  // itself (Task 10) -- see the banner rendered between the tab nav and <main> further down.
  //
  // The backend delivers the interrupt payload as a JSON *string* (ag_ui_langgraph's
  // dump_json_safe) -- parsing it is what makes the gate/escalation distinction work at all.
  // Discrimination is presence of `type`: the plain approval gate payload (graph.py
  // make_gate_node) has none; every escalation carries one.
  const interruptElement = useInterrupt<EscalationPayload, false>({
    agentId: localAgentId,
    renderInChat: false,
    render: ({ resolve, event }) => {
      const raw: unknown = event?.value;
      let payload: EscalationPayload = {};
      try {
        payload = (typeof raw === "string" ? JSON.parse(raw) : raw) ?? {};
      } catch {
        payload = {};
      }
      if (typeof payload !== "object" || payload === null) payload = {};
      return <InterruptCard payload={payload} resolve={resolve} />;
    },
  });

  // Reattach relaxation (fold-in fix, 2026-09-11): buildStarted(state) alone stays false for the
  // whole mid-run reattach gap (state.stages is empty by isReattaching's own definition), which
  // left this tab wrongly disabled on reload even when Build had genuinely started. `current_stage`
  // can't fill that gap alone either -- it only advances on a stage's OWN APPROVAL
  // (_run_post_approve_hook, graph.py), so it stays parked one stage behind while ac-to-tests is
  // still actively drafting, the single most common reattach moment. `runningStages` (event stream,
  // approval-independent) is the same backstop stageGroupDot/wasBuildRunningRef already lean on for
  // this identical lag, checked first; the stageOrderIndex comparison then covers the durably-known
  // "already past this stage, nothing currently running" case runningStages alone would miss.
  // Same reattach relaxation as buildTabEnabled just below, applied to the three earlier tabs
  // (user-reported live, thread 8242ea6d: reattached at Remediation with Requirements/
  // Specification/Plan all still disabled). Each stage's own live fields (state.stages?.[...],
  // ever_ready_for_review) are empty for the whole reattach gap same as buildStarted(state) is --
  // the pipeline only reaches a LATER stage after each earlier one is already approved, so a
  // durable current_stage past a given tab's own stage is sufficient on its own.
  const requirementsTabEnabled =
    state.stages?.["tech-stack"]?.status === "approved" ||
    state.stages?.["raw-requirements"] != null ||
    durableStageAtLeast("specification");
  const specificationTabEnabled =
    Boolean(specification?.ever_ready_for_review) ||
    Boolean(specification?.clarifying_questions?.length) ||
    durableStageAtLeast("plan");
  const planTabEnabled =
    Boolean(plan?.ever_ready_for_review) ||
    Boolean(plan?.clarifying_questions?.length) ||
    durableStageAtLeast("ac-to-tests");
  const buildTabEnabled =
    buildStarted(state) ||
    TAB_STAGE_GROUPS.build.some((k) => runningStages.has(k)) ||
    durableStageAtLeast("ac-to-tests");
  const qualityStarted =
    Boolean(state.stages?.remediation ?? state.stages?.["adversarial-compliance"] ?? state.test_hardening ?? state.metrics_report) ||
    TAB_STAGE_GROUPS.quality.some((k) => runningStages.has(k)) ||
    durableStageAtLeast("remediation");

  // User-reported (2026-09-11): Quality's tab dot stayed a pulsing blue "running" indefinitely
  // even once the Report tab was fully populated (remediation + adversarial-compliance long since
  // approved, metrics-exit approved too) -- root cause was this dot alone still using the coarse
  // pre-fix pattern qualityStarted/buildTabEnabled were already corrected away from above:
  // `agent.isRunning` is "SOME turn is in flight" (true for as long as ANY later stage -- test
  // hardening, e2e, metrics-exit -- keeps running), not "remediation/adversarial-compliance
  // specifically are what's running now"; and `state.metrics_report?.metrics != null` is a
  // completely different, much-later-populated bespoke field, not "are Quality's own two stages
  // approved." Every other tab's dot already delegates to stageGroupDot (same runningStages-first,
  // then status-derived done/awaiting/error logic Build/Spec/Plan/Tech-Stack already trust) --
  // Quality's dot never got that same treatment when it moved off the phantom
  // quality_remediation/security_remediation fields (root-caused 2026-09-11, see
  // workflow-types.ts's RemediationContent docstring) onto the real StageState-shaped
  // remediation/adversarial-compliance stages. Delegating here fixes both bugs at once with the
  // exact logic already proven correct for every sibling tab.
  const qualityDot = stageGroupDot(state, TAB_STAGE_GROUPS.quality, runningStages);

  // "metrics-exit" is the agent's real (post stage-stable-ids rename) key; "exit" is stale and
  // never populated (see runEnded()/workflow-types.ts's same dual-key check) -- reading only
  // `.exit` here left this tab's report/enabled/dot state permanently blind to every real run.
  const stagesForExit = (state.stages ?? {}) as Record<string, StageState | undefined>;
  const exitStage = stagesForExit["metrics-exit"] ?? stagesForExit["exit"];
  const reportEnabled =
    exitStage?.approved_content != null ||
    state.metrics_report?.metrics != null ||
    durableStageAtLeast("metrics-exit");
  const reportDot: DotState | undefined = exitStage?.approved_content != null ? "done" : undefined;

  const dots: Record<ViewId, DotState | undefined> = {
    "tech-stack": stageGroupDot(state, TAB_STAGE_GROUPS["tech-stack"], runningStages),
    requirements: stageGroupDot(state, TAB_STAGE_GROUPS.requirements, runningStages),
    specification: stageGroupDot(state, TAB_STAGE_GROUPS.specification, runningStages),
    plan: stageGroupDot(state, TAB_STAGE_GROUPS.plan, runningStages),
    build: stageGroupDot(state, TAB_STAGE_GROUPS.build, runningStages),
    quality: qualityDot,
    report: reportDot,
    overview: undefined,
  };

  return (
    <InterruptProvider>
      <div className="flex min-h-full flex-1 flex-col">
        {sandboxStatus === "error" && (
          <div className="border-b border-red-300 bg-red-50 px-4 py-2 text-sm text-red-900">
            Sandbox provisioning failed — the workflow can’t run. Reload the page to retry.
          </div>
        )}
        {/* Pre-build the bar's own scan chips only show the empty-repo baseline scan (a
            meaningless 89/A/Pass on zero code) -- misleading, per user feedback 2026-08-31 --
            MetricsBar's own summary-gated `chips` still suppress those regardless of this
            condition. This mount gate now also opens as soon as there's live cost to show
            (spec/plan already spend real tokens before Build starts), per the same 2026-09-01
            feedback that put `trailing` on its own always-eligible footing. */}
        {(buildTabEnabled || state.run_failure != null || runEvents.some((e) => e.token_usage != null)) && (
          <MetricsBar thresholds={metricThresholds} trailing={<LiveCostChip />} />
        )}
        <nav className="flex items-center gap-1 border-b border-neutral-200 px-4 py-2">
          <TabButton
            label="Tech Stack"
            active={activeView === "tech-stack"}
            dot={dots["tech-stack"]}
            onClick={() => setActiveView("tech-stack")}
          />
          <TabButton
            label="Requirements"
            active={activeView === "requirements"}
            // Tech-stack-first (product requirement 2026-08-31): requirements wait until the
            // stack is determined/selected. Legacy threads that already carry requirements
            // (raw-requirements stage exists) stay reachable regardless.
            disabled={!requirementsTabEnabled}
            dot={dots.requirements}
            onClick={() => setActiveView("requirements")}
          />
          <TabButton
            label="Specification"
            active={activeView === "specification"}
            disabled={!specificationTabEnabled}
            dot={dots.specification}
            onClick={() => setActiveView("specification")}
          />
          <TabButton
            label="Plan"
            active={activeView === "plan"}
            disabled={!planTabEnabled}
            dot={dots.plan}
            onClick={() => setActiveView("plan")}
          />
          <TabButton
            label="Build"
            active={activeView === "build"}
            disabled={!buildTabEnabled}
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
            label="Report"
            active={activeView === "report"}
            disabled={!reportEnabled}
            dot={dots.report}
            onClick={() => setActiveView("report")}
          />
          <TabButton
            label="Overview"
            active={activeView === "overview"}
            onClick={() => setActiveView("overview")}
          />
          {/* Session chrome lives HERE, not in WorkspaceHeader: that header mounts in root
              layout, OUTSIDE this page's SandboxStatusProvider, so a status pill there reads
              null context and renders nothing (found dead 2026-08-30). */}
          <div className="ml-auto flex items-center gap-2">
            {/* Global run indicator (user requirement 2026-08-31): visible on EVERY tab while
                the pipeline works. isRunning ONLY -- the spinner spins exactly while a run call
                to the agent is in flight (the attached stream). anyStageDrafting was removed
                (user, 2026-08-31): it reads server-state status flags, and a run that died
                mid-draft (agent restart, quota) leaves a stage stuck on "drafting" forever -- the
                spinner then claimed work that wasn't happening. Workflow Liveness Fix: OR'd with
                the durable run_active signal (agent/src/run_activity.py, via GET /sessions/{id})
                to close the "known mid-run reattach gap" this comment used to accept as a
                trade-off -- a reloaded client whose stream hasn't reattached yet, but whose
                server-side run genuinely is still active, now shows the spinner immediately
                instead of waiting for the stream. Suppressed while a review gate is open: the
                stream stays attached during a LangGraph interrupt, but the pipeline is waiting on
                the HUMAN then. */}
            {interruptElement == null && (agent.isRunning || runActivity?.runActive) && (
              <span className="flex items-center gap-1.5 text-xs text-neutral-500">
                <Spinner />
                {(() => {
                  const drafting = Object.entries(state.stages ?? {}).find(([, s]) => s?.status === "drafting")?.[0];
                  return drafting ? `${drafting} running…` : "working…";
                })()}
              </span>
            )}
            {/* Gated on a real push, not just the session row: the remote branch is created by
                the run's FIRST push, so linking earlier 404s (observed live at the tech-stack
                gate, 2026-08-31). last_push alone was too fragile -- it only lands in state when
                a gate node RETURNS, so mid-gate/reloaded clients hid the icon on branches that
                verifiably existed (backlog item 8). Any approved stage implies its approval
                commit was pushed, so that is the durable co-signal.
                ponytail: a failed push behind an approved stage still shows the icon (404 on
                click); upgrade path = persist branch-exists on dbo.sessions. */}
            {workBranch !== "" &&
              (state.last_push?.ok === true ||
                Object.values(state.stages ?? {}).some((s) => s?.status === "approved")) && (
              <a
                href={`https://github.com/${owner}/${repo}/tree/${workBranch.split("/").map(encodeURIComponent).join("/")}`}
                target="_blank"
                rel="noreferrer"
                title={`Open ${workBranch} on GitHub`}
                className="flex items-center rounded-md border border-neutral-200 p-1.5 text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900"
              >
                <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor" aria-label="GitHub branch">
                  <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
                </svg>
              </a>
            )}
            {/* Agent Narration Drawer trigger -- reuses the exact same "is a turn running" OR the
                global spinner above already computes, no new liveness signal invented. */}
            <button
              type="button"
              onClick={() => setNarrationOpen((v) => !v)}
              aria-label="Toggle agent activity drawer"
              aria-pressed={narrationOpen}
              className="relative flex items-center gap-1.5 rounded-md border border-neutral-200 px-2 py-1.5 text-xs text-neutral-600 hover:bg-neutral-100"
            >
              Activity
              {interruptElement == null && (agent.isRunning || runActivity?.runActive) && (
                <span aria-hidden className="h-2 w-2 rounded-full bg-blue-500 animate-pulse" />
              )}
            </button>
            <ContainerStatusButton
              status={sandboxStatus}
              stopping={stoppingContainer}
              onStop={async () => {
                if (!window.confirm("Stop this session's container? Its workspace volume is discarded — a later Resume re-provisions from the pushed branch.")) return;
                setStoppingContainer(true);
                try {
                  if (await terminateSession(threadId)) {
                    setSandboxStatus("terminated");
                    router.push("/select");
                  }
                } finally {
                  setStoppingContainer(false);
                }
              }}
            />
          </div>
        </nav>

        {/* Workflow Liveness Fix: a session can be `in_progress` (not yet a terminal DB status)
            with nothing actually executing it (process died, container killed, agent restarted --
            durable node events/persisted stage status all outlive the process, so nothing else in
            this file could tell). `interrupted` is server-computed and definitive; `status ===
            "failed"` is the other stopped-and-recoverable case.
            Pivot (root-caused 2026-09-12, user requirement): this banner used to carry its own
            Reattach/Resume button and a rewind-stage dropdown -- both fired the workflow directly,
            which is no longer allowed anywhere except SessionOverview's one stage-anchored restart
            button (that's the only place with the durable failure detail + real-stage mapping
            needed to target it correctly anyway). This is now informational only, pointing there.
            Hidden while already on Overview (root-caused 2026-09-12, user-reported): its whole
            point is "go see Overview", which is meaningless noise sitting right above that exact
            tab's own content. */}
        {activeView !== "overview" && ((runActivity?.interrupted && !isAwaitingFirstRequirements) || runFailed) && (
          <div className="flex items-center justify-between gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
            <span>
              {runFailed
                ? `This run failed and stopped${failedStageLabel ? ` at ${failedStageLabel}` : ""}.`
                : canReattach
                  ? "This run is still active, but you're not viewing its live progress right now."
                  : "This run appears to have stopped, and its progress can no longer be resumed."}{" "}
              See Overview for details and to continue.
            </span>
            <button
              type="button"
              className="shrink-0 rounded-md border border-amber-400 bg-white px-3 py-1 text-xs font-medium text-amber-900"
              onClick={() => setActiveView("overview")}
            >
              Go to Overview
            </button>
          </div>
        )}

        {/* Mid-run reattach banner (fold-in fix, 2026-09-11: was a full-page bg-white/95 cover on
            <main> that blocked every tab's live content, the only absolute-inset-0 blocker in this
            whole codebase -- every other "something's happening in the background" signal here
            (SandboxSessionBoot, ContainerStatus, the amber banner above) is this same thin,
            non-blocking bar convention. isReattaching's own semantics are unchanged: `state.stages`
            still empty, durable row confirms the run is genuinely in_progress past tech-stack, not
            interrupted. Placed here (not inside <main>) so it stacks with, rather than covers, the
            amber banner above -- mutually exclusive in practice since durableRow.status can't be
            both "in_progress" and "failed" at once. */}
        {isReattaching && (
          <div className="flex items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-1.5 text-xs text-neutral-500">
            <Spinner className="h-3.5 w-3.5" />
            <span>
              Reconnecting to your session — currently at{" "}
              <strong>
                {PIPELINE_STAGE_ORDER.find((s) => s.key === durableRow?.current_stage)?.label ??
                  durableRow?.current_stage}
              </strong>
              . The pipeline keeps running in the background; this page updates automatically.
            </span>
          </div>
        )}

        {/* The Gate UI's new home (Task 10) -- rendered here so it's visible above whichever tab
            is open, matching the comment on useInterrupt above. null for tech-stack's own gate
            (InterruptCard returns null there; TechStackView renders its own controls instead) and
            for the ordinary "nothing is paused right now" case, so this adds no dead space then. */}
        {/* No wrapper div: InterruptCard renders null for tech-stack's own gate, and a padded
            wrapper around that null was a 12px phantom gap above every view while that gate was
            open (user, 2026-08-31). The card's non-null returns carry their own mx-4 mt-3. */}
        {interruptElement}

        {/* Views stay MOUNTED and hide via [hidden] (backlog item 3, 2026-08-31): unmounting on
            tab switch reset unsaved editor text, dropdown picks, and scroll -- observed live.
            Every view already tolerates empty state (tabs enable mid-run), so mounting them all
            up front only costs idle renders. Reattach gap (backlog item 4, user found confusing
            live 2026-08-31): while isReattaching, every tab's own empty-state copy is honest again
            now that buildTabEnabled/qualityStarted are unblocked and BuildView's StageCard knows to
            say "Completed -- waiting for full detail to sync…" instead of a bare "Not started" for
            a stage current_stage confirms already finished (fold-in fix, 2026-09-11) -- the banner
            above this main covers the rest (which stage the durable row is at), so no per-tab guess
            needs covering up here anymore. Views stay mounted underneath (hidden, not unmounted) so
            they pick up state the instant a snapshot arrives, same as the tab-switch fix above. */}
        <main className="flex-1 overflow-y-auto">
          <div hidden={activeView !== "tech-stack"}><TechStackView /></div>
          <div hidden={activeView !== "requirements"}><RequirementsView /></div>
          <div hidden={activeView !== "specification"}><SpecificationView /></div>
          <div hidden={activeView !== "plan"}><PlanView /></div>
          <div hidden={activeView !== "build"}><BuildView /></div>
          <div hidden={activeView !== "quality"}><QualityView /></div>
          <div hidden={activeView !== "report"}>
            <ReportView
              report={exitStage?.approved_content as MergeReadinessReport | null | undefined}
              metricsExitStatus={exitStage?.status}
              deltaSummary={state.repo_scan?.delta_summary}
              filesChanged={filesChanged}
              screenshotUrls={state.e2e?.screenshots?.map((path) => rawProxyUrl(owner, repo, path, workBranch))}
              reportExtras={reportExtras}
            />
          </div>
          <div hidden={activeView !== "overview"}><SessionOverview owner={owner} repo={repo} branch={workBranch} /></div>
        </main>
        <AgentNarrationDrawer open={narrationOpen} onClose={() => setNarrationOpen(false)} />
      </div>
    </InterruptProvider>
  );
}

/** The chat-feed card for an open interrupt. A real component (not inline JSX in the render
 * prop) so hooks are legal: it publishes {open, stage, draft} into InterruptContext — Submit
 * gating and the post-reload draft fallback both hang off that. */
function InterruptCard({
  payload,
  resolve,
}: {
  payload: EscalationPayload;
  resolve: (value: unknown) => void;
}) {
  const { setInterrupt } = useOpenInterrupt();
  const stageKey = typeof payload.stage === "string" ? payload.stage : undefined;
  const stageLabel = PIPELINE_STAGE_ORDER.find((s) => s.key === stageKey)?.label ?? stageKey ?? "this stage";
  const draft = (payload as Record<string, unknown>).draft;
  const draftMarkdown = (payload as Record<string, unknown>).markdown;
  const fileExisted = (payload as Record<string, unknown>).file_existed;
  // Why a Reject would send the draft back for revision (Ruling 3, graph.py make_gate_node) --
  // required so the redraft has something to act on. No explicit reset needed between gate
  // occurrences: useInterrupt's own `element` is null while a rejected stage is redrafting (real
  // async work in between), so this whole component unmounts and a fresh instance -- fresh
  // useState("") included -- mounts for the next occurrence, same stage or not.
  const [feedback, setFeedback] = useState("");

  const done = (value: unknown) => {
    setInterrupt({ open: false });
    resolve(value);
  };

  useEffect(() => {
    setInterrupt({
      open: true,
      stage: stageKey,
      draft,
      draftMarkdown: typeof draftMarkdown === "string" ? draftMarkdown : undefined,
      fileExisted: typeof fileExisted === "boolean" ? fileExisted : undefined,
      resolve: done,
    });
    return () => setInterrupt({ open: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- payload identity churns per render; stage is the real key
  }, [stageKey]);

  // The Tech Stack tab handles its own review entirely -- it reads {draftMarkdown, fileExisted,
  // resolve} from InterruptContext directly (set above) rather than rendering a sidebar card.
  if (stageKey === "tech-stack") return null;

  if (payload.type) {
    const rest: Record<string, unknown> = { ...(payload as Record<string, unknown>) };
    delete rest.stage;
    delete rest.type;
    delete rest.draft; // huge; the views render it, not this card
    const text = [rest.feedback, rest.reason].find((v) => typeof v === "string" && v) as string | undefined;
    delete rest.feedback;
    delete rest.reason;
    return (
      <div className="mx-4 mt-3 space-y-2 rounded-lg border border-red-300 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-900">
          {stageLabel}: {String(payload.type).replaceAll("_", " ")}
        </p>
        {text && <p className="text-xs text-red-800">{text}</p>}
        {Object.keys(rest).length > 0 && (
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-xs text-red-800">
            {JSON.stringify(rest, null, 2)}
          </pre>
        )}
        {/* Scalar resume on purpose: an empty object is classified by LangGraph as an empty
            resume MAP, delivering no value -- the interrupt would re-raise forever. */}
        <button
          className="rounded-lg bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white"
          onClick={() => done("retry")}
        >
          Acknowledge &amp; retry
        </button>
      </div>
    );
  }

  // Requirements-as-single-source-of-truth (user ruling 2026-08-31, extended to Plan 2026-08-31):
  // neither the Specification nor the Plan gate has a Reject/feedback box -- change requests
  // belong in the requirements document, and the Requirements tab's Submit (live while either
  // gate is open) resolves the OPEN gate with the revised doc. For Plan specifically, that
  // resolve also carries graph.py's GraphState.restart_from_specification signal so the redraft
  // cascades through Specification first (Plan's own draft is built from the approved spec, not
  // raw requirements directly -- a plain loop-back-to-Plan's-own-draft would leave the revision
  // unreflected in what Plan actually reads); see make_route_after_gate's own docstring.
  if (stageKey === "specification" || stageKey === "plan") {
    const derivationCopy =
      stageKey === "specification"
        ? "this specification — and every plan, test, and line of code after it — is derived from that document alone"
        : "this plan is derived from the approved Specification, which is itself derived from that document alone";
    return (
      <div className="mx-4 mt-3 flex items-center justify-between gap-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
        <span className="text-sm text-amber-900">
          The <strong>{stageLabel}</strong> is ready for your review. Your{" "}
          <strong>Requirements document is the single source of truth</strong>: {derivationCopy}. Nothing
          you want will make it into the product unless it&apos;s written there. To change anything here,
          don&apos;t comment — edit the document on the Requirements tab and resubmit; {stageKey === "plan" ? "the specification and this plan are" : "the specification is"}{" "}
          redrafted from it, and every question it answers is traced back to your wording.
        </span>
        <button
          className="shrink-0 rounded-lg bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white"
          onClick={() => done({ decision: "approved" })}
        >
          Approve
        </button>
      </div>
    );
  }

  return (
    <div className="mx-4 mt-3 space-y-2 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <span className="text-sm text-amber-900">
          The <strong>{stageLabel}</strong> is ready for your review.
        </span>
        <div className="flex shrink-0 gap-2">
          <button
            className="rounded-lg bg-neutral-900 px-4 py-1.5 text-sm font-medium text-white"
            onClick={() => done({ decision: "approved" })}
          >
            Approve
          </button>
          <button
            className="rounded-lg border border-red-300 bg-white px-4 py-1.5 text-sm font-medium text-red-700 disabled:cursor-not-allowed disabled:opacity-40"
            disabled={!feedback.trim()}
            title={feedback.trim() ? undefined : "Add feedback below to explain what should change"}
            onClick={() => done({ decision: "rejected", feedback: feedback.trim() })}
          >
            Reject
          </button>
        </div>
      </div>
      {/* Required to reject (Ruling 3) -- the redraft this feeds (graph.py's make_gate_node ->
          the stage's own draft node) has nothing to act on otherwise. */}
      <textarea
        className="w-full rounded-md border border-amber-300 bg-white px-2 py-1 text-sm text-neutral-900 outline-none placeholder:text-neutral-400"
        rows={2}
        placeholder="What should change before this is approved? (required to reject)"
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
      />
    </div>
  );
}

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
      {dot === "running" ? (
        // A spinning icon, not just another colored dot -- an amber "awaiting" dot and a blue
        // "running" dot are too close in a quick glance at 8px (user feedback 2026-09-01: "unclear
        // which stage is running"). Shape + motion reads unambiguously where hue alone didn't.
        <RunningSpinner className="ml-1.5 h-2.5 w-2.5" />
      ) : (
        dot && <span aria-hidden className={`ml-1.5 inline-block h-2 w-2 rounded-full ${DOT_CLASS[dot]}`} />
      )}
    </button>
  );
}
