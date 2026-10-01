"use client";

import {
  UseAgentUpdate,
  useAgent,
  useCopilotKit,
  useInterrupt,
} from "@copilotkit/react-core/v2";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { BuildView } from "@/components/BuildView";
import { ContainerStatusButton } from "@/components/ContainerStatus";
import { GatePanel, GateSlot, gateViewId } from "@/components/GateSlot";
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
import { InterruptProvider, useOpenInterrupt, type InterruptVerification } from "@/lib/interrupt-context";
import { useCodeGenMode, usePipeline, type PipelineTab } from "@/lib/pipeline";
import { rawProxyUrl } from "@/lib/raw-proxy";
import { useSandboxStatus } from "@/lib/sandbox-status-context";
import { useRunActivity } from "@/lib/run-activity-context";
import { EMPTY_STAGES, useRunningStages, useStructuralRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { EscalationPayload, MergeReadinessReport, WorkflowState } from "@/lib/workflow-types";

type DotState = "running" | "done" | "error" | "awaiting";

const DOT_CLASS: Record<DotState, string> = {
  running: "bg-blue-500 animate-pulse",
  awaiting: "bg-amber-500 animate-pulse",
  done: "bg-emerald-500",
  error: "bg-red-500",
};

/** Dot for a tab, derived from its stages' ordinary StageStates. Green dots
 * intentionally clear on resubmission: intake resets later stages to not_started on each fresh
 * run, and the dots simply reflect that.
 *
 * `runningStages` (computeRunningStages, use-run-events.ts) backstops `status === "drafting"`:
 * a non-gated stage (ac-to-tests, minimal-code-to-green, ...) cycles through "ready_for_review"
 * between verify attempts -- a generic status name the backend reuses for "draft phase done"
 * regardless of whether a human is involved -- so relying on `status` alone showed a stage that
 * was actively retrying as "awaiting" almost the entire time (user feedback 2026-09-01). Checked
 * FIRST: the live event stream is more current than state, which only pushes on a gate pause. */
function stageGroupDot(
  state: WorkflowState,
  keys: string[],
  runningStages: Set<string>,
  // A failed verification under an ADVISORY gate policy (e.g. metrics-exit's in yolo) doesn't
  // block the run, so it must not paint the tab red.
  isAdvisory: (stageKey: string) => boolean,
): DotState | undefined {
  // Checked before the stages.length guard below: mid-run reattach (user feedback 2026-09-01)
  // means `state.stages` can be completely empty for a while even though the run is genuinely
  // active -- the event stream still knows, so this must not wait on stage state existing at all.
  if (keys.some((k) => runningStages.has(k))) return "running";
  const present = keys.filter((k) => state.stages?.[k] != null);
  const stages = present.map((k) => state.stages![k]);
  if (stages.length === 0) return undefined;
  if (stages.some((s) => s.status === "drafting")) return "running";
  if (stages.some((s) => s.status === "ready_for_review" || s.status === "needs_clarification")) return "awaiting";
  const failed = (k: string) => {
    const s = state.stages![k];
    return Boolean(s.last_verification && !s.last_verification.passed && s.status !== "approved");
  };
  if (present.some((k) => failed(k) && !isAdvisory(k))) return "error";
  if (stages.every((s) => s.status === "approved")) return "done";
  return undefined;
}

/** Tabs whose content is a human-reviewed draft: they open once a draft is actually ready for
 * review (ever_ready_for_review / clarifying questions), not merely while it's drafting -- the
 * durable fallback is current_stage having moved PAST the tab's last stage. Keyed by view (the
 * bespoke component), not by stage key. */
const REVIEW_GATED_VIEWS = new Set(["specification", "plan"]); // stage-literal-ok: review-gated bespoke views

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
  // Live state's own mode wins; the session row's is the fallback; null = not known yet.
  const codeGenMode = useCodeGenMode(state.code_gen_mode);
  const runEvents = useStructuralRunEvents();
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
  // amber dot. Two modes:
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
  function durableStageAtLeast(target: string | undefined): boolean {
    if (target == null) return false;
    return stageOrderIndex(durableRow?.current_stage) >= stageOrderIndex(target);
  }

  // Requirement (root-caused 2026-09-12): "Resume picks up from the last checkpoint" named no
  // actual checkpoint -- a user had no way to tell what that even meant. Same lookup the reattach
  // banner just below already uses.
  const failedStageLabel = durableRow?.current_stage ? stageLabel(durableRow.current_stage) : null;

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

  // Tab enabled rule -- one generic rule over each backend tab's stages. Reattach relaxation
  // (fold-in fixes 2026-09-11/12): each stage's own live fields are empty for the whole mid-run
  // reattach gap, so `runningStages` (event stream, approval-independent) and the durable
  // current_stage (monotonic, survives failed/completed) each open a tab on their own. Once a
  // stage has ever executed, its tab must never become disabled again.
  //  - the first (landing) tab and stage-less tabs (Overview) are always open;
  //  - review-gated views (Specification, Plan) wait for a draft actually ready for review, or a
  //    durable current_stage PAST the tab's last stage (current_stage == X can mean "X drafting");
  //  - every other tab opens when any of its stages has a status past not_started (intake
  //    pre-creates every stage at not_started), is running, or current_stage reached its first
  //    stage; or once all of enable_after are approved (Requirements: tech-stack-first); or once
  //    one of its bespoke enable_state_keys is present (Quality: test_hardening/metrics_report).
  function tabEnabled(tab: PipelineTab, index: number): boolean {
    if (index === 0 || tab.stages.length === 0) return true;
    const keys = tab.stages.map((st) => st.key);
    const stageOf = (k: string) => state.stages?.[k];
    const readyForReview = keys.some(
      (k) => Boolean(stageOf(k)?.ever_ready_for_review) || Boolean(stageOf(k)?.clarifying_questions?.length),
    );
    if (REVIEW_GATED_VIEWS.has(tab.view)) {
      return readyForReview || durableStageAtLeast(pipeline.nextStageAfter(keys[keys.length - 1]));
    }
    return (
      readyForReview ||
      keys.some((k) => (stageOf(k)?.status ?? "not_started") !== "not_started") ||
      keys.some((k) => runningStages.has(k)) ||
      durableStageAtLeast(keys[0]) ||
      (tab.enable_after.length > 0 && tab.enable_after.every((k) => stageOf(k)?.status === "approved")) ||
      tab.enable_state_keys.some((k) => (state as Record<string, unknown>)[k] != null)
    );
  }
  const enabled: Record<string, boolean> = Object.fromEntries(tabs.map((t, i) => [t.id, tabEnabled(t, i)]));
  const buildTab = tabs.find((t) => t.view === "build");
  const buildTabEnabled = buildTab != null && enabled[buildTab.id];

  // Focus follows running work: a stage newly entering runningStages (false->true edge, so it
  // never fights a manual tab click made while that stage keeps running -- found live 2026-09-01:
  // landed on Plan while Build was active) switches to its tab, if that tab is open. Catches the
  // non-gated stages that spend most of their active time in "ready_for_review" between verify
  // attempts rather than "drafting" (the status-cycling flaw stageGroupDot backstops too), so
  // they win the landing race against a same-tick stale gate. Furthest newly-running stage wins.
  const enabledRef = useRef(enabled);
  useEffect(() => {
    enabledRef.current = enabled;
  });
  const prevRunningRef = useRef<Set<string>>(EMPTY_STAGES);
  useEffect(() => {
    const prev = prevRunningRef.current;
    prevRunningRef.current = runningStages;
    const target = [...runningStages]
      .filter((k) => !prev.has(k))
      .sort((a, b) => stageOrderIndex(b) - stageOrderIndex(a))
      .map((k) => tabForStage(k))
      .find((t) => t != null && enabledRef.current[t.id]);
    // setState-in-effect: reacting to the live event stream (an external store).
    if (target) setActiveView(target.id);
  }, [runningStages, stageOrderIndex, tabForStage]);

  // Stable reference across unrelated re-renders (ReportView is React.memo'd) -- a plain inline
  // `.map()` in the JSX below would allocate a new array every AppShell render regardless of
  // whether the screenshot list itself changed, silently defeating that memo.
  const screenshotUrls = useMemo(
    () => state.e2e?.screenshots?.map((path) => rawProxyUrl(owner, repo, path, workBranch)),
    [state.e2e?.screenshots, owner, repo, workBranch],
  );

  const isAdvisory = (k: string) => pipeline.gatePolicyFor(k, codeGenMode) === "advisory";
  const dots: Record<string, DotState | undefined> = Object.fromEntries(
    tabs.map((t) => [t.id, stageGroupDot(state, t.stages.map((st) => st.key), runningStages, isAdvisory)]),
  );
  const stageKeys = useMemo(
    () => Object.fromEntries(tabs.map((t) => [t.id, t.stages.map((st) => st.key)])),
    [tabs],
  );
  const viewContext: ViewContext = {
    owner, repo, workBranch, state, stageKeys, scrollRequest, screenshotUrls, filesChanged, reportExtras,
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
          <MetricsBar thresholds={metricThresholds} trailing={<LiveCostChip />} onJumpToSection={jumpToQualitySection} />
        )}
        <nav className="flex items-center gap-1 overflow-x-auto border-b border-neutral-200 px-4 py-2">
          <div role="tablist" className="flex items-center gap-1">
            {tabs.map((tab) => (
              <Fragment key={tab.id}>
                <TabButton
                  label={tab.label}
                  active={activeView === tab.id}
                  disabled={!enabled[tab.id]}
                  dot={dots[tab.id]}
                  onClick={() => setActiveView(tab.id)}
                />
                {/* The gate between this tab and the next is a tab too (GateSlot.tsx). */}
                <GateSlot
                  tab={tab}
                  codeGenMode={codeGenMode}
                  active={activeView === gateViewId(tab)}
                  onSelect={() => setActiveView(gateViewId(tab))}
                />
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
            {interruptElement == null && (agent.isRunning || runActivity?.runActive) && (
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
        {tabs.find((t) => t.id === activeView)?.view !== "overview" && ((runActivity?.interrupted && !isAwaitingFirstRequirements) || runFailed) && (
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
              onClick={() => setActiveView(tabs.find((t) => t.view === "overview")?.id ?? defaultTabId)}
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
                {stageLabel(durableRow?.current_stage)}
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
          {tabs.map((tab) => (
            <div key={tab.id} role="tabpanel" hidden={activeView !== tab.id}>
              {(VIEWS[tab.view] ?? GenericStageView)(tab, viewContext)}
            </div>
          ))}
          {/* Gate screens mount only while selected: they hold no live-run state to keep warm, and
              remounting re-fetches attempt history. */}
          {tabs.map((tab) =>
            activeView === gateViewId(tab) ? (
              <div key={gateViewId(tab)} role="tabpanel">
                <GatePanel tab={tab} codeGenMode={codeGenMode} owner={owner} repo={repo} />
              </div>
            ) : null,
          )}
        </main>
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
  const stageLabel = usePipeline().stageLabel(stageKey) || "this stage";
  const draft = (payload as Record<string, unknown>).draft;
  const draftMarkdown = (payload as Record<string, unknown>).markdown;
  const fileExisted = (payload as Record<string, unknown>).file_existed;
  const verification = (payload as Record<string, unknown>).verification as InterruptVerification | null | undefined;
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
      verification: verification ?? null,
      resolve: done,
    });
    return () => setInterrupt({ open: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- payload identity churns per render; stage is the real key
  }, [stageKey]);

  // The Tech Stack tab handles its own review entirely -- it reads {draftMarkdown, fileExisted,
  // resolve} from InterruptContext directly (set above) rather than rendering a sidebar card.
  if (stageKey === "tech-stack") return null; // stage-literal-ok: Tech Stack handles its own gate

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
  if (stageKey === "specification" || stageKey === "plan") { // stage-literal-ok: InterruptCard source-of-truth copy
    const derivationCopy =
      stageKey === "specification" // stage-literal-ok: InterruptCard source-of-truth copy
        ? "this specification — and every plan, test, and line of code after it — is derived from that document alone"
        : "this plan is derived from the approved Specification, which is itself derived from that document alone";
    return (
      <div className="mx-4 mt-3 flex items-center justify-between gap-4 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
        <span className="text-sm text-amber-900">
          The <strong>{stageLabel}</strong> is ready for your review. Your{" "}
          <strong>Requirements document is the single source of truth</strong>: {derivationCopy}. Nothing
          you want will make it into the product unless it&apos;s written there. To change anything here,
          don&apos;t comment — edit the document on the Requirements tab and resubmit; {stageKey === "plan" /* stage-literal-ok: InterruptCard source-of-truth copy */ ? "the specification and this plan are" : "the specification is"}{" "}
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
      type="button"
      role="tab"
      aria-selected={active}
      className={[
        "flex shrink-0 items-center whitespace-nowrap rounded-md px-3 py-1.5 text-sm font-medium",
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
