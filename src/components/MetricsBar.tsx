"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { PIPELINE_STAGE_ORDER, type WorkflowState } from "@/lib/workflow-types";

/** Threshold pairs read server-side from env (see page.tsx) so they're runtime-configurable --
 * NEXT_PUBLIC_* would be baked in at build time and unchangeable in a deployed image. */
export interface MetricThresholds {
  healthGreen: number;
  healthAmber: number;
  coverageGreen: number;
  coverageAmber: number;
  dupGreen: number;
  dupAmber: number;
  secGreen: number;
  secAmber: number;
}

const CHIP_CLASS: Record<"green" | "amber" | "red", string> = {
  green: "border-emerald-300 bg-emerald-50 text-emerald-800",
  amber: "border-amber-300 bg-amber-50 text-amber-800",
  red: "border-red-300 bg-red-50 text-red-800",
};

function grade(value: number, green: number, amber: number, invert = false): "green" | "amber" | "red" {
  if (invert) return value <= green ? "green" : value <= amber ? "amber" : "red";
  return value >= green ? "green" : value >= amber ? "amber" : "red";
}

function Chip({ label, value, tone }: { label: string; value: string; tone: "green" | "amber" | "red" }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs ${CHIP_CLASS[tone]}`}>
      <span className="font-medium">{label}</span>
      {value}
    </span>
  );
}

const STATUS_LABEL: Record<string, string> = {
  drafting: "drafting",
  needs_clarification: "needs clarification",
  ready_for_review: "ready for review",
};

/** TurboTax-style always-visible metrics strip. Chips light up progressively as their metric
 * first streams in over AG-UI state; the whole bar stays hidden until the first metric exists. */
export function MetricsBar({ thresholds }: { thresholds: MetricThresholds }) {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const scan = state.repo_scan;

  const summary = scan?.latest_summary ?? scan?.baseline_summary;
  const health = summary?.health_score;

  const coverage = scan?.coverage?.line_rate ?? state.metrics_report?.metrics?.coverage?.line_rate ?? null;

  const bySeverity = summary?.by_severity ?? {};
  const securityCount = summary ? (bySeverity["critical"] ?? 0) + (bySeverity["high"] ?? 0) : null;

  const duplication =
    scan?.latest_duplication_percent ?? state.quality_remediation?.duplication_percent ?? null;

  const activeStage = PIPELINE_STAGE_ORDER.find((s) => {
    const status = state.stages?.[s.key]?.status;
    return status != null && status !== "not_started" && status !== "approved";
  });

  const chips: React.ReactNode[] = [];
  if (health != null) {
    chips.push(<Chip key="health" label="Repo health" value={`${health}/100`} tone={grade(health, thresholds.healthGreen, thresholds.healthAmber)} />);
  }
  if (coverage != null) {
    chips.push(<Chip key="cov" label="Coverage" value={`${coverage.toFixed(0)}%`} tone={grade(coverage, thresholds.coverageGreen, thresholds.coverageAmber)} />);
  }
  if (securityCount != null) {
    chips.push(<Chip key="sec" label="Security" value={`${securityCount} high+`} tone={grade(securityCount, thresholds.secGreen, thresholds.secAmber, true)} />);
  }
  if (duplication != null) {
    chips.push(<Chip key="dup" label="Duplication" value={`${duplication}%`} tone={grade(duplication, thresholds.dupGreen, thresholds.dupAmber, true)} />);
  }

  // The status/push lines matter before any metric exists (a needs_clarification stage was
  // previously invisible exactly when no scan had streamed) -- only hide a truly empty strip.
  if (chips.length === 0 && !activeStage && state.last_push?.ok !== false && state.run_failure == null) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-1.5">
      {chips}
      {activeStage && (
        <span className="ml-auto text-xs text-neutral-500">
          {activeStage.label} — {STATUS_LABEL[state.stages?.[activeStage.key]?.status ?? ""] ?? state.stages?.[activeStage.key]?.status}
        </span>
      )}
      {state.last_push && state.last_push.ok === false && (
        <span className="rounded-full border border-red-300 bg-red-50 px-2.5 py-0.5 text-xs text-red-800">
          push failing — GitHub persistence off
        </span>
      )}
      {state.run_failure && (
        <span className="rounded-full border border-red-300 bg-red-50 px-2.5 py-0.5 text-xs text-red-800">
          {state.run_failure.stage}: {state.run_failure.type} — run ended
          {state.run_failure.type === "cannot_verify" ? " (sandbox lost — resubmit to restart)" : ""}
        </span>
      )}
    </div>
  );
}
