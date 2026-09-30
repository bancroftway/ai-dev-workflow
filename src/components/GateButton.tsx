"use client";

import { UseAgentUpdate, useAgent } from "@copilotkit/react-core/v2";
import { Shield, ShieldAlert, ShieldBan, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { deriveGateRows, type GateRow, type GateVerdict, type ReportedCheck, type RowTone } from "@/lib/gate-rows";
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

const STATUS_LOOK: Record<GateStatus, { text: string; badge: string; className: string; Icon: typeof Shield }> = {
  off: { text: "not enforced", badge: "", className: "text-neutral-400 opacity-50", Icon: ShieldBan },
  unknown: { text: "mode not known yet", badge: "?", className: "text-neutral-500", Icon: Shield },
  not_run: { text: "not run yet", badge: "–", className: "text-neutral-400", Icon: Shield },
  verifying: { text: "verifying", badge: "…", className: "text-blue-600 animate-pulse", Icon: Shield },
  passed: { text: "passed", badge: "✓", className: "text-green-600", Icon: ShieldCheck },
  failed: { text: "failed", badge: "✕", className: "text-red-600", Icon: ShieldAlert },
  warn: { text: "needs attention", badge: "!", className: "text-amber-600", Icon: ShieldAlert },
};

const TONE_CLASS: Record<RowTone, string> = {
  pass: "text-green-700",
  fail: "text-red-700 font-medium",
  warn: "text-amber-700",
  muted: "text-neutral-500",
};

// ponytail: the descriptor has no per-mode "runs the audit" flag, so this mirrors graph.py's
// _audit_enabled (mission_critical only). Move to PipelineMode when the backend exposes it.
const auditOnFor = (mode: string | null | undefined) => (mode ? mode === "mission_critical" : undefined);

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
  let status: GateStatus;
  let statusText: string | undefined;
  if (verifying) status = "verifying";
  else if (verdict?.cannot_verify) [status, statusText] = ["warn", "cannot verify (no sandbox)"];
  else if (verdict?.passed) status = "passed";
  else if (verdict) [status, statusText] = policy === "advisory" ? ["warn", "advisory failure"] : ["failed", undefined];
  else if (policy === "off") status = "off";
  else if (policy === undefined) status = "unknown";
  else if (stageState?.status === "approved") [status, statusText] = ["not_run", "approved earlier, not re-verified"];
  else status = "not_run";
  return { stage, policy, stageState, verdict, lap, maxLaps, status, statusText: statusText ?? STATUS_LOOK[status].text };
}

/** The gate icon between stage tabs, and its per-check dialog (plan §6). */
export function GateButton({
  stages,
  codeGenMode,
  label,
  owner,
  repo,
}: {
  stages: PipelineStage[];
  codeGenMode: string | null;
  /** The tab's label, used when the tab gates more than one stage. */
  label: string;
  owner: string;
  repo: string;
}) {
  const pipeline = usePipeline();
  const { localAgentId, threadId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId, updates: [UseAgentUpdate.OnStateChanged, UseAgentUpdate.OnRunStatusChanged] });
  const state = (agent.state ?? {}) as WorkflowState;
  const [runActivity] = useRunActivity();
  const sharedPhases = useRunningPhases();
  const phases = runActivity?.runActive === false ? EMPTY_PHASES : sharedPhases;
  const { interrupt } = useOpenInterrupt();
  const [open, setOpen] = useState(false);

  const views = stages.map((s) =>
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
  const look = STATUS_LOOK[worst.status];
  const name = stages.length === 1 ? stages[0].label : label;
  const policyText = [...new Set(views.map((v) => v.policy ?? "mode unknown"))].join("/");
  const aria = `${name} verification: ${policyText}, ${worst.statusText}`;

  return (
    <>
      <button
        type="button"
        aria-label={aria}
        title={aria}
        onClick={() => setOpen(true)}
        className={`relative flex shrink-0 items-center rounded-md p-1 hover:bg-neutral-100 ${look.className}`}
      >
        <look.Icon className="size-4" aria-hidden />
        {look.badge && (
          <span aria-hidden className="absolute -right-0.5 -bottom-0.5 text-[9px] leading-none font-bold">
            {look.badge}
          </span>
        )}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-5xl max-h-[85vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>{name} verification</DialogTitle>
            <DialogDescription>
              Every deterministic check this gate runs, and what the latest (or a past) attempt recorded.
            </DialogDescription>
          </DialogHeader>
          {open && <GateDialogBody views={views} codeGenMode={codeGenMode} threadId={threadId} owner={owner} repo={repo} />}
        </DialogContent>
      </Dialog>
    </>
  );
}

/** Mounted only while the dialog is open, so history + insights are fetched once per open. */
function GateDialogBody({
  views,
  codeGenMode,
  threadId,
  owner,
  repo,
}: {
  views: StageView[];
  codeGenMode: string | null;
  threadId: string;
  owner: string;
  repo: string;
}) {
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
    <div className="flex flex-col gap-6">
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
  const [selected, setSelected] = useState<number | null>(null); // null = Latest
  const attempt = selected != null ? attempts?.[selected] : undefined;
  const mode = attempt ? attempt.code_gen_mode : codeGenMode;
  const modeLabel = mode ? (pipeline.modes.find((m) => m.id === mode)?.label ?? mode) : null;
  const policy = attempt ? attempt.policy : view.policy;
  const verdict: GateVerdict | null = attempt ? { passed: attempt.stage_passed, checks: attempt.checks } : view.verdict;
  const rows = deriveGateRows({
    checks: view.stage.gate?.checks ?? [],
    wrapperChecks: pipeline.descriptor.wrapper_checks,
    verdict,
    policy,
    modeLabel,
    auditOn: auditOnFor(mode),
    stageStatus: attempt ? null : (view.stageState?.status ?? null),
    lap: view.lap,
  });
  const verdictText = verdict == null ? "no verdict yet" : verdict.cannot_verify ? "cannot verify" : verdict.passed ? "passed" : "failed";
  const failRate = (id: string) => insights?.checks.find((c) => c.check_id === id && c.stage === view.stage.key);

  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <h3 className="font-semibold">{view.stage.label}</h3>
        <span className="text-neutral-600">Mode: {modeLabel ?? "not known yet"}</span>
        <span className="text-neutral-600">Policy: {policy ?? "—"}</span>
        {!attempt && view.maxLaps > 0 && (
          <span className="text-neutral-600">
            lap {view.lap} of {view.maxLaps}
          </span>
        )}
        <span className="text-neutral-600">Verdict: {verdictText}</span>
        <label className="ml-auto flex items-center gap-1.5 text-neutral-600">
          Attempt
          <select
            className="rounded-md border border-neutral-300 px-2 py-1 text-sm"
            value={selected ?? ""}
            onChange={(e) => setSelected(e.target.value === "" ? null : Number(e.target.value))}
            disabled={!attempts?.length}
          >
            <option value="">Latest</option>
            {attempts?.map((a, i) => (
              <option key={`${a.run_id}:${a.attempt}`} value={i}>
                {i + 1} · {a.stage_passed ? "passed" : "failed"} · {new Date(a.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!attempt && view.verdict?.feedback && !view.verdict.passed && (
        <p className="rounded-md bg-neutral-50 p-2 text-xs whitespace-pre-wrap text-neutral-700">{view.verdict.feedback}</p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-neutral-200 text-neutral-500">
            <tr>
              {["#", "Check", "What it verifies", "Mode", "Condition", "Status", "Detail", "Source"].map((h) => (
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
                    <td colSpan={8} className="px-2 pt-3 pb-1 font-medium text-neutral-600">
                      {group === "wrapper" ? "Around every verify" : "Reported but not in this gate's catalog"}
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

function CheckRow({ row, n, stats }: { row: GateRow; n: number; stats: VerifyInsights["checks"][number] | undefined }) {
  return (
    <tr className="border-b border-neutral-100 align-top">
      <td className="px-2 py-1.5 text-neutral-400">{n}</td>
      <td className="px-2 py-1.5">
        <div className="font-medium text-neutral-800">{row.label}</div>
        {row.uncatalogued && (
          <span className="mt-0.5 inline-block rounded bg-amber-100 px-1.5 text-[10px] text-amber-800">uncatalogued</span>
        )}
        {stats && stats.runs > 0 && (
          <div className="text-[11px] text-neutral-500">
            fails in {Math.round(stats.fail_rate * 100)}% of runs in this repo
          </div>
        )}
      </td>
      <td className="px-2 py-1.5 text-neutral-600">{row.description}</td>
      <td className="px-2 py-1.5 text-neutral-600">{row.mode}</td>
      <td className="px-2 py-1.5 text-neutral-600">{row.condition}</td>
      <td className={`px-2 py-1.5 whitespace-nowrap ${TONE_CLASS[row.tone]}`}>
        {row.text}
        {row.lapNote && <div className="text-[11px] font-normal text-neutral-500">{row.lapNote}</div>}
      </td>
      <td className="max-w-xs px-2 py-1.5 text-neutral-700">
        {row.detail && (
          <details>
            <summary className="cursor-pointer truncate">{row.detail.split("\n")[0]}</summary>
            <pre className="mt-1 whitespace-pre-wrap break-words font-sans">{row.detail}</pre>
          </details>
        )}
      </td>
      <td className="px-2 py-1.5 text-[11px] break-all text-neutral-400">{row.source}</td>
    </tr>
  );
}
