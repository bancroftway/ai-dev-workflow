"use client";

import {
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
} from "@copilotkit/react-core/v2";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { BuildView } from "@/components/BuildView";
import { ContainerStatusButton } from "@/components/ContainerStatus";
import { GateButton, GateView, gateViewId, useTabStrip, type TabTone } from "@/components/GateButton";
import { useRecovery } from "@/components/RecoveryPanel";
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
import { usePipeline, type PipelineTab } from "@/lib/pipeline";
import { rawProxyUrl } from "@/lib/raw-proxy";
import { ReviewProvider, useReview, useReviewModel } from "@/lib/review-context";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useRunActivity } from "@/lib/run-activity-context";
import { EMPTY_STAGES, useRunningStages, useStructuralRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { MergeReadinessReport, WorkflowState } from "@/lib/workflow-types";

// The server's tab tone (gate_view._tab_tone) as the tab label's own colour; same
// green/red/amber as the gate icons. Lighter tints on the active tab's dark background.
// "running" shows a spinner instead; "none" keeps the default colour.
const LABEL_CLASS: Partial<Record<TabTone, { idle: string; active: string }>> = {
  awaiting: { idle: "text-amber-600", active: "text-amber-300" },
  done: { idle: "text-emerald-600", active: "text-emerald-300" },
  error: { idle: "text-red-600", active: "text-red-300" },
};

type ViewContext = {
  owner: string;
  repo: string;
  workBranch: string;
  state: WorkflowState;
  /** Per-tab memoised stage-key arrays (BuildView is memo'd). */
  stageKeys: Record<string, string[]>;
  scrollRequest: { section: string } | null;
  screenshotUrls: string[] | undefined;
  filesChanged?: FilesChangedSummary | null;
  reportExtras?: ReportExtras | null;
};

/** StageCards for a tab's stages -- the Tests/Code tabs' view, and the fallback for any backend
 * tab whose view key has no bespoke component here, so a new stage at least appears (with its
 * gate) without a frontend change. */
function GenericStageView(tab: PipelineTab, c: ViewContext) {
  return <BuildView title={tab.label} stageKeys={c.stageKeys[tab.id]} />;
}

/** Backend TabSpec.view -> the component that renders it. */
const VIEWS: Record<string, (tab: PipelineTab, ctx: ViewContext) => ReactNode> = {
  "tech-stack": () => <TechStackView />, // stage-literal-ok: VIEWS registry key (view id, not a stage list)
  requirements: (_tab, c) => <RequirementsView owner={c.owner} repo={c.repo} workBranch={c.workBranch} />,
  specification: () => <SpecificationView />,
  plan: () => <PlanView />,
  build: GenericStageView,
  quality: (_tab, c) => <QualityView scanFindings={c.reportExtras?.findings} scrollRequest={c.scrollRequest} />,
  report: (tab, c) => {
    // The Report tab's own stage, or an old session's pre-rename "exit" key (legacy_labels) --
    // the live key is metrics-exit; reading only one left this tab blind to the other.
    const key = tab.stages[0]?.key;
    const exitStage = (key ? c.state.stages?.[key] : undefined) ?? c.state.stages?.exit;
    return (
      <ReportView
        report={exitStage?.approved_content as MergeReadinessReport | null | undefined}
        metricsExitStatus={exitStage?.status}
        deltaSummary={c.state.repo_scan?.delta_summary}
        filesChanged={c.filesChanged}
        screenshotUrls={c.screenshotUrls}
        reportExtras={c.reportExtras}
      />
    );
  },
  overview: (_tab, c) => <SessionOverview owner={c.owner} repo={c.repo} branch={c.workBranch} />,
};

/** sessions_api SessionResponse.notice: a stopped run's notice and its one action. */
interface RunNotice {
  tone: "error" | "warn";
  text: string;
  action: { id: "open_gate" | "open_overview" | "resume"; label: string; tab_id?: string };
}

const NOTICE_CLASS: Record<RunNotice["tone"], string> = {
  error: "border-red-300 bg-red-50 text-red-900",
  warn: "border-amber-300 bg-amber-50 text-amber-900",
};

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
  // Tabs, stage order and labels all come from the backend pipeline descriptor (PipelineProvider).
  const pipeline = usePipeline();
  const { tabs, stageOrderIndex, stageLabel, tabForStage } = pipeline;
  const defaultTabId = tabs[0]?.id ?? "";
  const [activeView, setActiveView] = useState<string>(defaultTabId);
  const { copilotkit } = useCopilotKit();
  const [sandboxStatus, setSandboxStatus] = useSandboxStatus();
  // Declared early (not down by the poll that populates it) so runningStages below can read it --
  // `null` until that poll's first response arrives; see computeRunningStages' own tri-state note.
  const [runActivity, setRunActivity] = useRunActivity();
  const router = useRouter();
  // GitHub-branch icon (root-caused 2026-09-16, user-reported: icon never appeared even once
  // tech-stack was approved and the branch existed in GitHub). `workBranch` is resolved once,
  // server-side, by the workflow page (see its own prop doc above) -- for a BRAND NEW session,
  // that server render can happen before SandboxSessionBoot's client-side provision call has
  // created the row/branch at all, freezing workBranch at "" for this page's entire lifetime
  // (Next.js Server Components render once per navigation; nothing here ever re-fetches). Once
  // provisioning finishes, `sandboxStatus` flips to "ready" -- the one signal available client-side
  // that the row (and its work_branch) now genuinely exists -- so refresh the server-rendered props
  // exactly once when that happens, but only if workBranch actually still looks stale (empty);
  // a resumed/already-provisioned session's non-empty workBranch never needs this.
  const branchRefreshedRef = useRef(false);
  useEffect(() => {
    if (workBranch !== "" || sandboxStatus !== "ready" || branchRefreshedRef.current) return;
    branchRefreshedRef.current = true;
    router.refresh();
  }, [workBranch, sandboxStatus, router]);
  const [stoppingContainer, setStoppingContainer] = useState(false);
  // MetricsBar pill -> Quality tab section navigation. A fresh object literal on every click (not
  // a request id/counter) is deliberate: MetricsBar is visible on every tab, so clicking the SAME
  // pill twice in a row while already on Quality must still re-trigger the scroll/highlight, and
  // `{section}` is never Object.is-equal to its predecessor either way. Nothing ever needs to
  // clear this back to null.
  const [scrollRequest, setScrollRequest] = useState<{ section: string } | null>(null);
  const jumpToQualitySection = (section: string) => {
    const qualityTab = tabs.find((t) => t.view === "quality");
    if (qualityTab) setActiveView(qualityTab.id);
    setScrollRequest({ section });
  };

  const state = (agent.state ?? {}) as WorkflowState;
  const runEvents = useStructuralRunEvents();
  // Each tab's label, enabled flag, status tone and gate icon -- decided server-side.
  const tabStrip = useTabStrip();
  // The open human review, from the server (review-context.tsx) -- shared with every view below.
  const reviewState = useReviewModel();
  const review = reviewState.review;
  const enabled = useMemo(() => Object.fromEntries(tabStrip.map((t) => [t.tab_id, t.enabled])), [tabStrip]);
  const sharedRunningStages = useRunningStages();
  const runningStages = runActivity?.runActive === false ? EMPTY_STAGES : sharedRunningStages;
  // Always-fresh handle for effects below whose own deps intentionally exclude `state` (recreating
  // a poll's setInterval on every state tick would be wasteful) but still need this render's value.
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = (agent.state ?? {}) as WorkflowState;
  }, [agent.state]);

  // Focus follows the pipeline (user ask 2026-08-31): when a stage starts needing the user (a
  // gate opens) or a new phase begins, switch to its tab instead of making the user chase the
  // amber tab label. Two modes:
  //  - TRANSITION: a stage's status changed this mount -> jump to the mapped tab.
  //  - FIRST LOAD (no previous statuses seen): land on the most relevant tab for the state as
  //    hydrated -- a returning user opens where the action is, not on the Tech Stack default.
  // Manual clicks always win afterwards: auto-switches only ever fire on fresh transitions.
  const stagesForFocus = state.stages;
  // Ordered latest-phase-first (furthest stage in run order): the furthest stage that newly needs
  // attention wins. A rule whose trigger stage lives in ANOTHER tab (tech-stack:approved ->
  // Requirements) is a "go to the next thing" jump: it only fires while the target tab's own stages
  // are all still not_started -- resumed/delta threads that already carry requirements skip it.
  // Checked by STATUS, not key presence: intake pre-creates every stage entry at not_started.
  const focusRules = useMemo(
    () =>
      pipeline.tabs
        .flatMap((tab) =>
          tab.focus_on.map((rule) => {
            const at = rule.lastIndexOf(":");
            const key = rule.slice(0, at);
            const own = tab.stages.map((st) => st.key);
            return { key, at: rule.slice(at + 1), to: tab.id, untouched: own.includes(key) ? [] : own };
          }),
        )
        .sort((a, b) => pipeline.stageOrderIndex(b.key) - pipeline.stageOrderIndex(a.key)),
    [pipeline],
  );
  const prevStageStatusRef = useRef<Record<string, string> | null>(null);
  useEffect(() => {
    if (stagesForFocus == null || Object.keys(stagesForFocus).length === 0) return;
    const stages = stagesForFocus as Record<string, { status?: string } | undefined>;
    const status = (key: string) => stages[key]?.status;
    const prev = prevStageStatusRef.current;
    const current: Record<string, string> = {};
    for (const [key, stage] of Object.entries(stages)) if (stage?.status) current[key] = stage.status;
    prevStageStatusRef.current = current;

    // Each tab's backend focus_on ("<stage>:<status>") -- see focusRules above.
    const RULES = focusRules.map((r) => ({
      ...r,
      also: () => r.untouched.every((k) => (stages[k]?.status ?? "not_started") === "not_started"),
    }));
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
  }, [stagesForFocus, focusRules]);

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
    if (activeView !== defaultTabId) return; // already navigated (manually or by a sibling effect)
    // The tab of current_stage itself, not the stage after it: current_stage is written both at a
    // stage's draft START and at its approval (graph.py make_draft_node/_run_post_approve_hook),
    // so it is usually the stage in flight (or paused at its gate) right now.
    const tab = tabForStage(runActivity.currentStage)?.id;
    // setState-in-effect is the point here, same exemption as the RULES effect above: reacting to
    // a durable SERVER value (current_stage), not to derivable render-time data.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (tab && tab !== defaultTabId) setActiveView(tab);
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
    failure_gate: { tab_id: string; button: string } | null;
    notice: RunNotice | null;
  } | null>(null);
  // Exposes the durable-row stream's own reconnect-if-closed check to the run-start effect below
  // (root-caused 2026-09-11): that stream's "done" handler closes it for good on a REAL
  // terminal status, with no reopen logic of its own -- by design, since most "done"s (completed)
  // truly are final. But "failed" is also terminal-shaped here, and failed sessions CAN be
  // resumed (the run notice, a gate row's Retry/Redo, Overview) -- and once
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
      review_id?: string | null;
      failure_gate?: { tab_id: string; button: string } | null;
      notice?: RunNotice | null;
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
        failure_gate: row.failure_gate ?? null, notice: row.notice ?? null,
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
        reviewId: row.review_id ?? null,
        failureGate: row.failure_gate ?? null,
      });
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
  }, [threadId, setSandboxStatus, setRunActivity]);

  // A run starting is proof the durable row is no longer terminal: the "done" handler above closed
  // the stream when the run failed, and only a window focus reopened it -- after a Retry/Redo the
  // failure notice and Overview rows stayed stale until the user happened to alt-tab (2026-10-01).
  // Whatever started the run (gate row action, Overview, review approve), reopen it right away.
  useEffect(() => {
    if (agent.isRunning) reconnectDurableRowIfClosedRef.current();
  }, [agent.isRunning]);

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

  // A review that opened while this tab wasn't attached to the run (agent restart, reload
  // mid-run, another tab's action): the draft under review exists only in the checkpoint -- pull
  // it, once per review. Skipped while a live run streams state into this tab anyway.
  const hydratedReviewRef = useRef<string | null>(null);
  useEffect(() => {
    if (!agentIsReady || !review.open || review.id == null || agent.isRunning) return;
    if (hydratedReviewRef.current === review.id) return;
    hydratedReviewRef.current = review.id;
    fetch(`/api/sessions/${encodeURIComponent(threadId)}/checkpoint-state`)
      .then((res) => (res.ok ? (res.json() as Promise<Record<string, unknown>>) : null))
      .then((snapshot) => {
        if (snapshot && Object.keys(snapshot).length > 0 && !agent.isRunning) agent.setState(snapshot);
      })
      .catch(() => {});
  }, [threadId, agent, agentIsReady, agent.isRunning, review.open, review.id]);

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
    durableRow.current_stage !== pipeline.order[0] &&
    !runActivity?.interrupted;

  // Workflow Liveness Fix false positive (found live 2026-09-06): graph.py's
  // _route_after_repo_scan_baseline deliberately ends the run at END -- not a gate, no
  // interrupt() -- once tech-stack is approved but no requirements have been typed yet ("waits
  // for the Requirements tab", per that router's own docstring). That leaves status=in_progress,
  // run_active=false, awaiting_gate=false: textbook `interrupted` by sessions_api.py's
  // definition, even though nothing crashed -- every fresh session sits in exactly this state
  // right after approving its tech stack. Generic form: some tab's enable_after stages are all
  // approved (Requirements' "tech stack first") while its own stages are all still not_started --
  // the same pair the focus rule above trusts to mean "waiting on the human, not broken".
  const isAwaitingFirstRequirements = tabs.some(
    (t) =>
      t.enable_after.length > 0 &&
      t.enable_after.every((k) => state.stages?.[k]?.status === "approved") &&
      t.stages.every((st) => (state.stages?.[st.key]?.status ?? "not_started") === "not_started"),
  );

  // The run notice's actions run through the same recovery engine as Overview's panel and the
  // gate rows (one confirm-and-restart implementation; ensureSandboxProvisioned covers a dead
  // container before resuming).
  const recovery = useRecovery(owner, repo, workBranch);

  // Requirement (root-caused 2026-09-12): "Resume picks up from the last checkpoint" named no
  // actual checkpoint -- a user had no way to tell what that even meant. Same lookup the reattach
  // banner just below already uses.

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

  const buildTab = tabs.find((t) => t.view === "build");
  const buildTabEnabled = buildTab != null && enabled[buildTab.id];

  // Focus follows running work: a stage newly entering runningStages (false->true edge, so it
  // never fights a manual tab click made while that stage keeps running -- found live 2026-09-01:
  // landed on Plan while Build was active) switches to its tab, if that tab is open. Catches the
  // non-gated stages that spend most of their active time in "ready_for_review" between verify
  // attempts rather than "drafting", so they win the landing race against a same-tick stale gate.
  // Furthest newly-running stage wins. The server's tab strip trails the event stream by one
  // refetch, so a newly-running stage whose tab isn't open YET stays pending until it opens (or
  // the stage stops running) instead of the edge being lost.
  const prevRunningRef = useRef<Set<string>>(EMPTY_STAGES);
  const pendingRunningRef = useRef<string[]>([]);
  useEffect(() => {
    const prev = prevRunningRef.current;
    prevRunningRef.current = runningStages;
    const pending = [...pendingRunningRef.current, ...[...runningStages].filter((k) => !prev.has(k))].filter((k) =>
      runningStages.has(k),
    );
    const target = pending
      .sort((a, b) => stageOrderIndex(b) - stageOrderIndex(a))
      .map((k) => tabForStage(k))
      .find((t) => t != null && enabled[t.id]);
    pendingRunningRef.current = target ? [] : pending;
    // setState-in-effect: reacting to the live event stream (an external store).
    if (target) setActiveView(target.id);
  }, [runningStages, enabled, stageOrderIndex, tabForStage]);

  // Stable reference across unrelated re-renders (ReportView is React.memo'd) -- a plain inline
  // `.map()` in the JSX below would allocate a new array every AppShell render regardless of
  // whether the screenshot list itself changed, silently defeating that memo.
  const screenshotUrls = useMemo(
    () => state.e2e?.screenshots?.map((path) => rawProxyUrl(owner, repo, path, workBranch)),
    [state.e2e?.screenshots, owner, repo, workBranch],
  );

  const stageKeys = useMemo(
    () => Object.fromEntries(tabs.map((t) => [t.id, t.stages.map((st) => st.key)])),
    [tabs],
  );
  const viewContext: ViewContext = {
    owner, repo, workBranch, state, stageKeys, scrollRequest, screenshotUrls, filesChanged, reportExtras,
  };

  return (
    <ReviewProvider value={reviewState}>
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
          <MetricsBar thresholds={metricThresholds} trailing={<LiveCostChip />} onJumpToSection={jumpToQualitySection} />
        )}
        <nav className="flex items-center gap-1 overflow-x-auto border-b border-neutral-200 px-4 py-2">
          <div role="tablist" className="flex items-center gap-[6.8px]">
            {tabStrip.map((tab) => (
              <Fragment key={tab.tab_id}>
                <TabButton
                  label={tab.label}
                  active={activeView === tab.tab_id}
                  disabled={!tab.enabled}
                  tone={tab.tone}
                  onClick={() => setActiveView(tab.tab_id)}
                />
                {/* The gate between this tab and the next is a tab too; the server says which tabs
                    gate something (GateButton.tsx). */}
                {tab.gate && (
                  <GateButton
                    icon={tab.gate.icon}
                    active={activeView === gateViewId(tab.tab_id)}
                    onSelect={() => setActiveView(gateViewId(tab.tab_id))}
                  />
                )}
              </Fragment>
            ))}
          </div>
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
            {!review.open && (agent.isRunning || runActivity?.runActive) && (
              <span className="flex items-center gap-1.5 text-xs text-neutral-500">
                <Spinner />
                {(() => {
                  const drafting = Object.entries(state.stages ?? {}).find(([, s]) => s?.status === "drafting")?.[0];
                  return drafting ? `${stageLabel(drafting)} running…` : "working…";
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

        {/* The run notice (server-built, sessions_api SessionResponse.notice / gate_view.run_notice):
            what stopped and the ONE action that gets the run going -- open the gate whose failed row
            carries Retry/Redo, open Overview, or resume an interrupted run (the same confirm-and-
            resume Overview's row offers, via useRecovery). Shown on every tab except the place its
            own action leads to. Replaced a banner that read "still active" whenever the container
            was up, even with nothing executing the run (after an agent restart, 2026-10-01). */}
        {(() => {
          const notice = durableRow?.notice;
          if (!notice) return null;
          const { action } = notice;
          const overviewId = tabs.find((t) => t.view === "overview")?.id ?? defaultTabId;
          const target = action.id === "open_gate" && action.tab_id ? gateViewId(action.tab_id) : action.id === "open_overview" ? overviewId : null;
          if (target != null && activeView === target) return null;
          if (action.id === "resume" && isAwaitingFirstRequirements) return null;
          const resumeAt = recovery.boundaryKey ?? durableRow?.current_stage ?? null;
          return (
            <div className={`flex items-center justify-between gap-3 border-b px-4 py-2 text-sm ${NOTICE_CLASS[notice.tone] ?? ""}`}>
              <span>{notice.text}</span>
              <button
                type="button"
                className="shrink-0 rounded-md bg-neutral-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
                disabled={recovery.restarting || (action.id === "resume" && resumeAt == null)}
                onClick={() => {
                  if (target != null) setActiveView(target);
                  else if (resumeAt != null) void recovery.handleRestart(resumeAt);
                }}
              >
                {recovery.restarting ? "Working…" : action.label}
              </button>
            </div>
          );
        })()}

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
                {stageLabel(durableRow?.current_stage)}
              </strong>
              . The pipeline keeps running in the background; this page updates automatically.
            </span>
          </div>
        )}

        {/* The open review's card, above whichever tab is open -- built server-side
            (review_view.py), rendered as-is. Nothing when no review is open, or when the stage's
            own tab renders it (Tech Stack: `card` false). No wrapper div: a padded wrapper around
            nothing was a 12px phantom gap above every view (user, 2026-08-31). */}
        <ReviewCard key={review.id ?? "closed"} />

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
          {tabs.map((tab) => (
            <div key={tab.id} role="tabpanel" hidden={activeView !== tab.id}>
              {(VIEWS[tab.view] ?? GenericStageView)(tab, viewContext)}
            </div>
          ))}
          {/* Gate screens mount only while selected: they hold no live-run state to keep warm, and
              remounting re-fetches attempt history. */}
          {tabStrip.map((tab) =>
            activeView === gateViewId(tab.tab_id) && tab.gate ? (
              <div key={gateViewId(tab.tab_id)} role="tabpanel">
                <GateView tabId={tab.tab_id} owner={owner} repo={repo} branch={workBranch} />
              </div>
            ) : null,
          )}
        </main>
      </div>
    </ReviewProvider>
  );
}

const REVIEW_TONE = {
  review: { box: "border-amber-300 bg-amber-50", text: "text-amber-900", input: "border-amber-300" },
  error: { box: "border-red-300 bg-red-50", text: "text-red-900", input: "border-red-300" },
} as const;

const ACTION_CLASS = {
  primary: "bg-neutral-900 text-white",
  danger: "border border-red-300 bg-white text-red-700",
} as const;

/** The open review's card (review_view.py builds title/body/actions; this paints them). Every
 * button posts its action id; the server maps it to the graph's resume value. */
function ReviewCard() {
  const { review, submitting, error, submit } = useReview();
  // The reviewer's text (reject feedback). AppShell keys this component by review id, so each
  // review starts empty.
  const [text, setText] = useState("");
  if (!review.open || !review.card) return null;
  const tone = REVIEW_TONE[review.tone ?? "review"];
  const status = error ?? review.blocked ?? review.note ?? null;
  return (
    <div className={`mx-4 mt-3 space-y-2 rounded-lg border px-4 py-3 ${tone.box}`}>
      <div className="flex items-center justify-between gap-4">
        <div className={`space-y-1 text-sm ${tone.text}`}>
          {review.title && <p className="font-medium">{review.title}</p>}
          {(review.body?.length ?? 0) > 0 && (
            <p className={review.title ? "text-xs" : undefined}>
              {review.body?.map((s, i) => (s.bold ? <strong key={i}>{s.text}</strong> : <Fragment key={i}>{s.text}</Fragment>))}
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          {review.actions?.map((a) => {
            const missingText = a.needs_text && !text.trim();
            return (
              <button
                key={a.id}
                type="button"
                className={`rounded-lg px-4 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 ${ACTION_CLASS[a.style] ?? ACTION_CLASS.primary}`}
                disabled={!a.enabled || submitting || missingText}
                title={missingText ? (a.hint ?? undefined) : undefined}
                onClick={() => void submit(a.id, a.needs_text ? text.trim() : undefined)}
              >
                {a.label}
              </button>
            );
          })}
        </div>
      </div>
      {review.details && (
        <pre className={`max-h-40 overflow-auto whitespace-pre-wrap text-xs ${tone.text}`}>{review.details}</pre>
      )}
      {review.input && (
        <textarea
          className={`w-full rounded-md border bg-white px-2 py-1 text-sm text-neutral-900 outline-none placeholder:text-neutral-400 ${tone.input}`}
          rows={2}
          placeholder={review.input.placeholder}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      )}
      {status && <p className="text-xs text-neutral-600">{status}</p>}
    </div>
  );
}

function TabButton({
  label,
  active,
  disabled,
  tone,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  tone: TabTone;
  onClick: () => void;
}) {
  const toneClass = LABEL_CLASS[tone];
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={[
        "flex shrink-0 items-center whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium",
        active ? "bg-neutral-900" : "hover:bg-neutral-100",
        toneClass ? toneClass[active ? "active" : "idle"] : active ? "text-white" : "text-neutral-700",
        disabled ? "cursor-not-allowed opacity-40 hover:bg-transparent" : "",
      ].join(" ")}
      disabled={disabled}
      onClick={onClick}
    >
      {label}
      {tone === "running" ? (
        // A spinning icon, not just another colored dot -- an amber "awaiting" dot and a blue
        // "running" dot are too close in a quick glance at 8px (user feedback 2026-09-01: "unclear
        // which stage is running"). Shape + motion reads unambiguously where hue alone didn't.
        <RunningSpinner className="ml-1.5 h-2.5 w-2.5" />
      ) : null}
    </button>
  );
}
