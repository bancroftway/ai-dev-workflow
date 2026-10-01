"use client";

import { UseAgentUpdate, useAgent } from "@copilotkit/react-core/v2";
import { useEffect, useState } from "react";
import { deriveGateRows, fmt, type GateRow, type GateVerdict, type ReportedCheck, type RowTone } from "@/lib/gate-rows";
import { useOpenInterrupt } from "@/lib/interrupt-context";
import { usePipeline, type GatePolicy, type Pipeline, type PipelineStage } from "@/lib/pipeline";
import { useRunActivity } from "@/lib/run-activity-context";
import { EMPTY_PHASES, useRunningPhases } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { StageState, WorkflowState } from "@/lib/workflow-types";

/** Mirrors verify_check_store.list_attempts (GET /sessions/{id}/verify-history). */
interface VerifyAttempt {
  run_id: string;
  stage: string;
  attempt: number;
  timing: string;
  code_gen_mode: string | null;
  policy: GatePolicy;
  stage_passed: boolean;
  created_at: string;
  checks: ReportedCheck[];
}

/** Mirrors verify_check_store.check_stats (GET /repos/{owner}/{repo}/verify-insights). */
export interface VerifyInsights {
  checks: { check_id: string; stage: string; runs: number; fails: number; infra: number; fail_rate: number; last_failed: string | null }[];
  stages: { stage: string; avg_attempts_to_pass: number | null; sessions: number }[];
}

type GateStatus = "off" | "unknown" | "not_run" | "verifying" | "passed" | "failed" | "warn";

// Worst-first across a tab's gated stages (Quality has two).
const STATUS_RANK: GateStatus[] = ["off", "unknown", "not_run", "passed", "warn", "failed", "verifying"];

/** An arrow passing through a slatted gate -- the work passing a stage's checks. Stroke uses
 * currentColor, so the status colour classes below tint it. */
function GateArrowIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinejoin="round" className={className} aria-hidden>
      {/* arrow: shaft into a head */}
      <path d="M1 8h17V4l13 6-13 6v-4H1z" />
      {/* gate: a slanted frame with vertical slats, drawn over the shaft */}
      <path d="M5 4.5 11 1.5v16L5 19.5z" />
      <path d="M7 3.5v15M9 2.5v15" />
    </svg>
  );
}

// Status text comes from the descriptor's gate_text.icon_status; only glyphs and colours live here.
const STATUS_LOOK: Record<GateStatus, { badge: string; className: string }> = {
  off: { badge: "", className: "text-neutral-400 opacity-50" },
  unknown: { badge: "?", className: "text-neutral-500" },
  not_run: { badge: "", className: "text-neutral-400" },
  verifying: { badge: "…", className: "text-blue-600 animate-pulse" },
  passed: { badge: "✓", className: "text-green-600" },
  failed: { badge: "✕", className: "text-red-600" },
  warn: { badge: "!", className: "text-amber-600" },
};

const TONE_CLASS: Record<RowTone, string> = {
  pass: "text-green-700",
  fail: "text-red-700 font-medium",
  warn: "text-amber-700",
  muted: "text-neutral-500",
};


interface StageView {
  stage: PipelineStage;
  policy: GatePolicy | undefined;
  stageState: StageState | undefined;
  verdict: (GateVerdict & { feedback?: string }) | null;
  lap: number;
  maxLaps: number;
  status: GateStatus;
  statusText: string;
}

function stageView(
  stage: PipelineStage,
  state: WorkflowState,
  codeGenMode: string | null,
  pipeline: Pipeline,
  verifying: boolean,
  interruptVerdict: { verdict: GateVerdict & { feedback?: string }; attempts: number; max: number } | null,
): StageView {
  const policy = pipeline.gatePolicyFor(stage.key, codeGenMode);
  const stageState = state.stages?.[stage.key];
  const verdict = interruptVerdict?.verdict ?? stageState?.last_verification ?? null;
  const lap = interruptVerdict?.attempts ?? stageState?.verify_cycle_count ?? 0;
  const maxLaps = interruptVerdict?.max ?? stageState?.max_verify_cycles ?? 0;
  const T = pipeline.descriptor.gate_text.icon_status;
  let status: GateStatus;
  let statusText: string | undefined;
  if (verifying) status = "verifying";
  else if (verdict?.cannot_verify) [status, statusText] = ["warn", T.no_sandbox];
  else if (verdict?.passed) status = "passed";
  else if (verdict) [status, statusText] = policy === "advisory" ? ["warn", T.advisory_failure] : ["failed", undefined];
  else if (policy === "off") status = "off";
  else if (policy === undefined) status = "unknown";
  else if (stageState?.status === "approved") [status, statusText] = ["passed", T.approved_earlier];
  else status = "not_run";
  return { stage, policy, stageState, verdict, lap, maxLaps, status, statusText: statusText ?? T[status] };
}

/** Live per-stage gate state for a tab's gated stages, shared by the tab-strip icon and the gate
 * screen so both always agree. */
function useGateViews(stages: PipelineStage[], codeGenMode: string | null, label: string) {
  const pipeline = usePipeline();
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const state = (agent.state ?? {}) as WorkflowState;
  const [runActivity] = useRunActivity();
  const sharedPhases = useRunningPhases();
  const phases = runActivity?.runActive === false ? EMPTY_PHASES : sharedPhases;
  const { interrupt } = useOpenInterrupt();

  // A tab can gate an optional stage (brownfield-spec on Specification) that most sessions never
  // run; intake still creates its StageState as not_started. Once a sibling has progressed, an
  // untouched stage is not part of this session -- listing it would show "Will run" forever and
  // drag the tab's icon to "not run yet".
  const untouched = (s: PipelineStage) => {
    const st = state.stages?.[s.key];
    return !st || (st.status === "not_started" && st.last_verification == null);
  };
  const shown = stages.some((s) => !untouched(s)) ? stages.filter((s) => !untouched(s)) : stages;

  const views = shown.map((s) =>
    stageView(
      s,
      state,
      codeGenMode,
      pipeline,
      phases.get(s.key) === "verify",
      interrupt.open && interrupt.stage === s.key && interrupt.verification
        ? { verdict: interrupt.verification, attempts: interrupt.verification.attempts, max: interrupt.verification.max_attempts }
        : null,
    ),
  );
  const worst = views.reduce((a, b) => (STATUS_RANK.indexOf(b.status) > STATUS_RANK.indexOf(a.status) ? b : a));
  const name = shown.length === 1 ? shown[0].label : label;
  const gt = pipeline.descriptor.gate_text;
  const policyText = [...new Set(views.map((v) => v.policy ?? gt.policy_unknown))].join("/");
  return { views, worst, name, aria: fmt(gt.icon_aria, { name, policy: policyText, status: worst.statusText }) };
}

/** The gate between stage tabs: a tab in its own right -- selecting it shows GateView. */
export function GateButton({
  stages,
  codeGenMode,
  label,
  active,
  onSelect,
}: {
  stages: PipelineStage[];
  codeGenMode: string | null;
  /** The tab's label, used when the tab gates more than one stage. */
  label: string;
  active: boolean;
  onSelect: () => void;
}) {
  const { worst, aria } = useGateViews(stages, codeGenMode, label);
  const look = STATUS_LOOK[worst.status];
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      aria-label={aria}
      title={aria}
      onClick={onSelect}
      className={`relative flex shrink-0 items-center rounded-md p-1 hover:bg-neutral-100 ${look.className} ${
        active ? "bg-neutral-100 ring-2 ring-neutral-900" : ""
      }`}
    >
      <GateArrowIcon className="h-[18.4px] w-[27.6px]" />
      {look.badge && (
        <span aria-hidden className="absolute -right-0.5 -bottom-0.5 text-[9px] leading-none font-bold">
          {look.badge}
        </span>
      )}
    </button>
  );
}

/** The gate's screen. Mounted only while its tab is selected, so attempt history and repo
 * insights are re-fetched each time it is opened. */
export function GateView({
  stages,
  codeGenMode,
  label,
  owner,
  repo,
}: {
  stages: PipelineStage[];
  codeGenMode: string | null;
  label: string;
  owner: string;
  repo: string;
}) {
  const { views, name } = useGateViews(stages, codeGenMode, label);
  const gt = usePipeline().descriptor.gate_text;
  const { threadId } = useWorkflowThread();
  const [history, setHistory] = useState<VerifyAttempt[] | null>(null);
  const [insights, setInsights] = useState<VerifyInsights | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/sessions/${encodeURIComponent(threadId)}/verify-history`)
      .then((r) => (r.ok ? (r.json() as Promise<{ attempts: VerifyAttempt[] }>) : { attempts: [] }))
      .catch(() => ({ attempts: [] as VerifyAttempt[] }))
      .then((b) => !cancelled && setHistory(b.attempts));
    fetch(`/api/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/verify-insights`)
      .then((r) => (r.ok ? (r.json() as Promise<VerifyInsights>) : null))
      .catch(() => null)
      .then((b) => !cancelled && setInsights(b));
    return () => {
      cancelled = true;
    };
  }, [threadId, owner, repo]);

  return (
    <div className="flex flex-col gap-6 p-6">
      <header>
        <h2 className="text-lg font-semibold">{fmt(gt.title, { name })}</h2>
        <p className="text-sm text-neutral-600">{gt.subtitle}</p>
      </header>
      {views.map((v) => (
        <StageSection
          key={v.stage.key}
          view={v}
          codeGenMode={codeGenMode}
          attempts={history?.filter((a) => a.stage === v.stage.key) ?? null}
          insights={insights}
        />
      ))}
    </div>
  );
}

function StageSection({
  view,
  codeGenMode,
  attempts,
  insights,
}: {
  view: StageView;
  codeGenMode: string | null;
  attempts: VerifyAttempt[] | null;
  insights: VerifyInsights | null;
}) {
  const pipeline = usePipeline();
  const gt = pipeline.descriptor.gate_text;
  const [selected, setSelected] = useState<number | null>(null); // null = default below
  // No live verdict (stage approved earlier, or reset by a rewind/new run): default to the newest
  // recorded attempt rather than a table of "not re-verified" rows the user must click away from.
  const hasLive = view.verdict != null;
  const effective = selected ?? (!hasLive && attempts?.length ? attempts.length - 1 : null);
  const attempt = effective != null ? attempts?.[effective] : undefined;
  const mode = attempt ? attempt.code_gen_mode : codeGenMode;
  const modeLabel = mode ? (pipeline.modes.find((m) => m.id === mode)?.label ?? mode) : null;
  const policy = attempt ? attempt.policy : view.policy;
  const verdict: GateVerdict | null = attempt ? { passed: attempt.stage_passed, checks: attempt.checks } : view.verdict;
  const rows = deriveGateRows({
    checks: view.stage.gate?.checks ?? [],
    // The wrapper rows come from the verify node; an after-submit gate (tech-stack) runs its checks
    // inside the review gate instead, so they would only ever read "Not recorded" there.
    wrapperChecks: view.stage.gate?.timing === "after_submit" ? [] : pipeline.descriptor.wrapper_checks,
    verdict,
    policy,
    modeLabel,
    auditOn: mode ? pipeline.modes.find((m) => m.id === mode)?.audit : undefined,
    stageStatus: attempt ? null : (view.stageState?.status ?? null),
    lap: view.lap,
    text: gt.row_status,
  });
  const verdictText =
    gt.verdict_values[verdict == null ? "none" : verdict.cannot_verify ? "cannot_verify" : verdict.passed ? "passed" : "failed"];
  const failRate = (id: string) => insights?.checks.find((c) => c.check_id === id && c.stage === view.stage.key);

  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <h3 className="font-semibold">{view.stage.label}</h3>
        <span className="text-neutral-600">{fmt(gt.mode, { mode: modeLabel ?? gt.mode_unknown })}</span>
        <span className="text-neutral-600">{fmt(gt.policy, { policy: policy ?? gt.policy_none })}</span>
        {!attempt && view.maxLaps > 0 && (
          <span className="text-neutral-600">{fmt(gt.lap, { lap: view.lap, max: view.maxLaps })}</span>
        )}
        <span className="text-neutral-600">{fmt(gt.verdict, { verdict: verdictText })}</span>
        <label className="ml-auto flex items-center gap-1.5 text-neutral-600">
          {gt.attempt}
          <select
            className="rounded-md border border-neutral-300 px-2 py-1 text-sm"
            value={effective ?? ""}
            onChange={(e) => setSelected(e.target.value === "" ? null : Number(e.target.value))}
            disabled={!attempts?.length}
          >
            {(hasLive || !attempts?.length) && <option value="">{gt.attempt_latest}</option>}
            {attempts?.map((a, i) => (
              <option key={`${a.run_id}:${a.attempt}`} value={i}>
                {fmt(gt.attempt_option, {
                  n: i + 1,
                  result: gt.attempt_result[a.stage_passed ? "passed" : "failed"],
                  when: new Date(a.created_at).toLocaleString(),
                })}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="text-xs text-neutral-500">
        {gt.legend.map((seg, i) =>
          seg.bold ? (
            <span key={i} className="font-medium">
              {seg.text}
            </span>
          ) : (
            seg.text
          ),
        )}
      </p>
      {!attempt && view.verdict?.feedback && !view.verdict.passed && (
        <p className="rounded-md bg-neutral-50 p-2 text-xs whitespace-pre-wrap text-neutral-700">{view.verdict.feedback}</p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-neutral-200 text-neutral-500">
            <tr>
              {gt.columns.map((h) => (
                <th key={h} className="px-2 py-1.5 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(["stage", "wrapper", "uncatalogued"] as const).map((group) => {
              const groupRows = rows.filter((r) => r.group === group);
              if (groupRows.length === 0) return null;
              return [
                group !== "stage" && (
                  <tr key={`${group}-heading`}>
                    <td colSpan={gt.columns.length} className="px-2 pt-3 pb-1 font-medium text-neutral-600">
                      {gt.group_heading[group]}
                    </td>
                  </tr>
                ),
                ...groupRows.map((r, i) => <CheckRow key={r.id} row={r} n={i + 1} stats={failRate(r.id)} />),
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** Longer (or multi-line) details collapse behind a disclosure; shorter ones show inline. */
const DETAIL_INLINE_CHARS = 160;

function CheckRow({ row, n, stats }: { row: GateRow; n: number; stats: VerifyInsights["checks"][number] | undefined }) {
  const gt = usePipeline().descriptor.gate_text;
  return (
    <tr className="border-b border-neutral-100 align-top">
      <td className="px-2 py-1.5 text-neutral-400">{n}</td>
      <td className="px-2 py-1.5">
        <div className="font-medium text-neutral-800">{row.label}</div>
        {row.uncatalogued && (
          <span className="mt-0.5 inline-block rounded bg-amber-100 px-1.5 text-[10px] text-amber-800">{gt.uncatalogued_badge}</span>
        )}
        {stats && stats.fails > 0 && (
          <div className="text-[11px] text-neutral-500">{fmt(gt.fail_rate, { pct: Math.round(stats.fail_rate * 100) })}</div>
        )}
      </td>
      <td className="px-2 py-1.5 text-neutral-600">{row.description}</td>
      <td className="px-2 py-1.5 text-neutral-600">{gt.check_effect[row.mode] ?? row.mode}</td>
      <td className="px-2 py-1.5 text-neutral-600">{row.condition}</td>
      <td className={`px-2 py-1.5 whitespace-nowrap ${TONE_CLASS[row.tone]}`}>
        {row.text}
        {row.lapNote && <div className="text-[11px] font-normal text-neutral-500">{row.lapNote}</div>}
      </td>
      <td className="max-w-xs px-2 py-1.5 text-neutral-700">
        {row.detail &&
          (row.detail.length <= DETAIL_INLINE_CHARS && !row.detail.includes("\n") ? (
            <span className="break-words">{row.detail}</span>
          ) : (
            <details>
              <summary className="cursor-pointer truncate">{row.detail.split("\n")[0]}</summary>
              <pre className="mt-1 whitespace-pre-wrap break-words font-sans">{row.detail}</pre>
            </details>
          ))}
      </td>
    </tr>
  );
}
