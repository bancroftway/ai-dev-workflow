"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import { PIPELINE_STAGE_ORDER, type E2EState, type ScanMeasures, type WorkflowState } from "@/lib/workflow-types";
import {
  GRADE_TONE,
  computeDelta,
  gradeHigherIsBetter,
  gradeLowerIsBetter,
  securityGrade,
  securityOpenCount,
  type Delta,
  type Grade,
  type Thresholds4,
  type Tone,
} from "@/lib/metric-grades";

/** Threshold bands read server-side from env (see page.tsx) so they're runtime-configurable --
 * NEXT_PUBLIC_* would be baked in at build time and unchangeable in a deployed image. Each is
 * [t0,t1,t2,t3]: A/B/C/D band edges (ascending for ccn/dup where lower is better, descending for
 * coverage where higher is better); band E is whatever's left past t3. */
export interface MetricThresholds {
  ccn: Thresholds4;
  coverage: Thresholds4;
  dup: Thresholds4;
}

const CHIP_CLASS: Record<Tone, string> = {
  green: "border-emerald-300 bg-emerald-50 text-emerald-800",
  amber: "border-amber-300 bg-amber-50 text-amber-800",
  red: "border-red-300 bg-red-50 text-red-800",
  gray: "border-neutral-300 bg-neutral-100 text-neutral-500",
};

export function Chip({ label, value, tone, title }: { label: string; value: string; tone: Tone; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs ${CHIP_CLASS[tone]}`}>
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

/** Appends the delta arrow + signed numeric change (when non-zero) to a chip's base value. */
function withDelta(base: string, delta: Delta | null): string {
  if (!delta) return base;
  return `${base} ${delta.arrow}${delta.text ? ` ${delta.text}` : ""}`;
}

/** Shared shape for the three banded metrics (Maintainability/Coverage/Duplication): grade a
 * value against thresholds, diff it against the baseline, render "--" gray when the value itself
 * is missing (old data pre-dating Task 4's `measures` block, or an unmeasured metric). */
function bandedChip(opts: {
  metricKey: string;
  label: string;
  value: number | null | undefined;
  baseValue: number | null | undefined;
  thresholds: Thresholds4;
  higherIsBetter: boolean;
  decimals: number;
  unit: string;
  hasBaseline: boolean;
  title: (value: number, grade: Grade) => string;
  placeholderTitle: string;
}): React.ReactNode {
  const { metricKey, label, value, baseValue, thresholds, higherIsBetter, decimals, unit, hasBaseline, title, placeholderTitle } = opts;
  if (value == null) return <Chip key={metricKey} label={label} value="—" tone="gray" title={placeholderTitle} />;
  const g = higherIsBetter ? gradeHigherIsBetter(value, thresholds) : gradeLowerIsBetter(value, thresholds);
  const delta = hasBaseline ? computeDelta(baseValue, value, higherIsBetter, decimals) : null;
  return (
    <Chip
      key={metricKey}
      label={label}
      value={withDelta(`${g} · ${value.toFixed(decimals)}${unit}`, delta)}
      tone={GRADE_TONE[g]}
      title={title(value, g)}
    />
  );
}

/** Security is categorical (worst_open_severity), not banded against numeric thresholds like the
 * other three, so it gets its own small chip builder rather than fitting bandedChip's shape. */
function securityChip(measures: ScanMeasures | undefined, baseMeasures: ScanMeasures | undefined, hasBaseline: boolean): React.ReactNode {
  if (!measures) return <Chip key="sec" label="Security" value="—" tone="gray" title="No scan data yet." />;
  const worst = measures.security.worst_open_severity;
  const openCount = securityOpenCount(measures.security.by_severity);
  const g = securityGrade(worst);
  const delta = hasBaseline ? computeDelta(baseMeasures && securityOpenCount(baseMeasures.security.by_severity), openCount, false) : null;
  return (
    <Chip
      key="sec"
      label="Security"
      value={withDelta(`${g} · ${openCount}`, delta)}
      tone={GRADE_TONE[g]}
      title={`Open security findings (vulnerabilities, leaked secrets, insecure code). Grade = worst open severity; fewer and less severe is better. ${openCount} open, worst: ${worst}.`}
    />
  );
}

/** e2e is a bespoke node cluster with no StageState of its own (see workflow-types.ts's E2EState
 * comment), so `activeStage` below never picks it up -- rendered as its own pill instead, styled
 * like the push-failing/run_failure pills. Silent once it settles into passed/skipped, the same
 * way a StageState pill goes quiet once approved. */
function e2ePill(e2e: E2EState | null | undefined): React.ReactNode {
  if (!e2e || e2e.status == null || e2e.status === "passed" || e2e.status === "skipped") return null;
  if (e2e.status === "running") {
    return (
      <span className={`rounded-full border px-2.5 py-0.5 text-xs ${CHIP_CLASS.gray}`}>
        e2e: running (attempt {e2e.attempt ?? 1})
      </span>
    );
  }
  const failed = e2e.failed_tests?.length ?? 0;
  const tone: Tone = (e2e.passed ?? 0) > 0 ? "amber" : "red";
  return (
    <span className={`rounded-full border px-2.5 py-0.5 text-xs ${CHIP_CLASS[tone]}`}>
      e2e: {failed}/{e2e.total ?? 0} failed
    </span>
  );
}

/** TurboTax-style always-visible metrics strip: five fixed chips (Security, Maintainability,
 * Coverage, Duplication, Gate) once any scan summary exists, "--" gray placeholders for whatever
 * a given summary doesn't carry. The whole bar stays hidden until there's a summary, an active
 * stage, or a push/run failure to show. */
export function MetricsBar({ thresholds }: { thresholds: MetricThresholds }) {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const scan = state.repo_scan;

  const summary = scan?.latest_summary ?? scan?.baseline_summary ?? null;
  const measures = summary?.measures;
  const baseMeasures = scan?.baseline_summary?.measures;
  const hasBaseline = scan?.baseline_summary != null && scan?.latest_summary != null;
  // Coverage's failure `reason` lives on the coverage state key parallel to whichever summary is
  // in play (RepoScanState.coverage for latest, .baseline_coverage before a latest scan exists),
  // not on `measures` -- see repo_scan.py's `{line_rate: null, reason}` shape.
  const coverageState = scan?.latest_summary ? scan?.coverage : scan?.baseline_coverage;

  const activeStage = PIPELINE_STAGE_ORDER.find((s) => {
    const status = state.stages?.[s.key]?.status;
    return status != null && status !== "not_started" && status !== "approved";
  });
  const e2ePillNode = e2ePill(state.e2e);

  let chips: React.ReactNode = null;
  if (summary) {
    const security = securityChip(measures, baseMeasures, hasBaseline);

    const maintainability = bandedChip({
      metricKey: "maint",
      label: "Maintainability",
      value: measures?.mean_ccn,
      baseValue: baseMeasures?.mean_ccn,
      thresholds: thresholds.ccn,
      higherIsBetter: false,
      decimals: 1,
      unit: "",
      hasBaseline,
      placeholderTitle: "No scan data yet.",
      title: (ccn) => {
        const [a, b, c, d] = thresholds.ccn;
        return `Average cyclomatic complexity per function — how tangled the code's control flow is; lower is easier to change safely. Mean CCN ${ccn.toFixed(1)} (A≤${a}, B≤${b}, C≤${c}, D≤${d}).`;
      },
    });

    const coverage = bandedChip({
      metricKey: "cov",
      label: "Coverage",
      value: measures?.coverage_line_rate,
      baseValue: scan?.baseline_coverage?.line_rate ?? baseMeasures?.coverage_line_rate,
      thresholds: thresholds.coverage,
      higherIsBetter: true,
      decimals: 0,
      unit: "%",
      hasBaseline,
      placeholderTitle: `Percentage of code lines executed by the test suite; higher means changes are safer to make. Unavailable: ${coverageState?.reason ?? "not measured"}.`,
      title: (rate) => {
        const branch = coverageState?.branch_rate;
        return `Percentage of code lines executed by the test suite; higher means changes are safer to make. Line rate ${rate.toFixed(0)}%, branch ${branch != null ? `${branch.toFixed(0)}%` : "—"}.`;
      },
    });

    const duplication = bandedChip({
      metricKey: "dup",
      label: "Duplication",
      value: measures?.duplication_percent,
      baseValue: baseMeasures?.duplication_percent,
      thresholds: thresholds.dup,
      higherIsBetter: false,
      decimals: 1,
      unit: "%",
      hasBaseline,
      placeholderTitle: "No scan data yet.",
      title: (dup) =>
        `Percentage of code duplicated across files; lower means fixes don't need repeating in copies. ${dup.toFixed(1)}% duplicated.`,
    });

    const gatingCount = summary.gating_count;
    const gate = (
      <Chip
        key="gate"
        label="Gate"
        value={gatingCount === 0 ? "Pass" : `Fail · ${gatingCount}`}
        tone={gatingCount === 0 ? "green" : "red"}
        title={`Quality gate: fails when any finding at/above the severity floor (or newly introduced quality issue) is open. ${gatingCount} gating findings.`}
      />
    );

    chips = (
      <>
        {security}
        {maintainability}
        {coverage}
        {duplication}
        {gate}
      </>
    );
  }

  // The status/push lines matter before any scan has streamed (a needs_clarification stage was
  // previously invisible exactly when no scan had streamed) -- only hide a truly empty strip.
  if (!summary && !activeStage && !e2ePillNode && state.last_push?.ok !== false && state.run_failure == null) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-neutral-200 bg-neutral-50 px-4 py-1.5">
      {chips}
      {activeStage && (
        <span className="ml-auto text-xs text-neutral-500">
          {activeStage.label} — {STATUS_LABEL[state.stages?.[activeStage.key]?.status ?? ""] ?? state.stages?.[activeStage.key]?.status}
        </span>
      )}
      {e2ePillNode}
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
