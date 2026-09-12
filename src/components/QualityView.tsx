"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { useMemo } from "react";
import { RebuildConnector } from "@/components/BuildView";
import { HealthBreakdown } from "@/components/HealthRing";
import { ViewContainer } from "@/components/ViewContainer";
import { useRunActivity } from "@/lib/run-activity-context";
import { computeRunningPhases, useRunEvents } from "@/lib/use-run-events";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import {
  REBUILD_PLACEMENTS,
  rebuildPhase,
  stageOrderIndex,
  type AdversarialComplianceReport,
  type PresenceList,
  type RemediationContent,
  type RemediationFinding,
  type WorkflowState,
} from "@/lib/workflow-types";

const SEVERITY_CLASS: Record<string, string> = {
  critical: "text-red-700 font-semibold",
  high: "text-red-600",
  error: "text-red-600",
  medium: "text-amber-600",
  warning: "text-amber-600",
  low: "text-neutral-500",
  info: "text-neutral-400",
};

const VERDICT_CLASS: Record<string, string> = {
  conforms: "text-emerald-600",
  minor_gaps: "text-amber-600",
  major_gaps: "text-red-600",
  fails_to_conform: "text-red-700 font-semibold",
};

// Same per-file-local convention as BuildView/SessionOverview/MetricsBar's own copies of this map
// (see BuildView's comment on why "ready_for_review" reads as "Auto-verifying" there but not
// everywhere) -- remediation/adversarial-compliance have no human gate, so "ready_for_review" here
// means the deterministic gate is actively checking the draft, not "awaiting a person".
const STATUS_LABEL: Record<string, string> = {
  not_started: "Not started",
  drafting: "Drafting",
  ready_for_review: "Verifying",
  approved: "Approved",
};

/** A PresenceList (agent/src/schemas.py) rendered as either a bullet list or its typed-absence
 * reason -- never a bare empty list, which would read as "nothing to show" instead of "nothing
 * found, here's why". */
function PresenceLines({ items }: { items: PresenceList }) {
  if (items.status === "absent") {
    return <p className="text-xs italic text-neutral-500">{items.reason || "None."}</p>;
  }
  return (
    <ul className="list-disc space-y-0.5 pl-4 text-xs text-neutral-700">
      {items.values.map((v, i) => (
        <li key={i}>{v}</li>
      ))}
    </ul>
  );
}

export function FindingsTable({ findings, decisions }: { findings: RemediationFinding[]; decisions?: Record<string, { decision: string }> }) {
  if (findings.length === 0) return <p className="text-xs text-neutral-500">No findings.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead>
          <tr className="border-b border-neutral-200 text-neutral-500">
            <th className="py-1 pr-3 font-medium">Severity</th>
            <th className="py-1 pr-3 font-medium">Rule</th>
            <th className="py-1 pr-3 font-medium">Location</th>
            <th className="py-1 pr-3 font-medium">Message</th>
            <th className="py-1 font-medium">Decision</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((f, i) => (
            <tr key={f.finding_key ?? f.id ?? i} className="border-b border-neutral-100 align-top">
              <td className={`py-1 pr-3 ${SEVERITY_CLASS[f.severity ?? ""] ?? "text-neutral-600"}`}>{f.severity ?? "—"}</td>
              <td className="py-1 pr-3 font-mono">{f.rule ?? f.category ?? "—"}</td>
              <td className="py-1 pr-3 font-mono">{f.file ? `${f.file}${f.line != null ? `:${f.line}` : ""}` : "—"}</td>
              <td className="py-1 pr-3 text-neutral-700">{f.message ?? "—"}</td>
              <td className="py-1 text-neutral-500">{decisions?.[f.finding_key ?? ""]?.decision ?? "open"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Section({ title, children, status }: { title: string; children: React.ReactNode; status?: React.ReactNode }) {
  return (
    <section className="space-y-2 rounded-lg border border-neutral-200 p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold">{title}</h2>
        {status}
      </div>
      {children}
    </section>
  );
}

export function QualityView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const tests = state.test_hardening;
  const metrics = state.metrics_report?.metrics;
  const scan = state.repo_scan;

  const baselineHealth = scan?.baseline_summary?.health_score;
  const latestHealth = scan?.latest_summary?.health_score;

  // remediation/adversarial-compliance are real StageState-shaped stages (graph.py's StageSpec),
  // not the phantom quality_remediation/security_remediation fields this view used to read (those
  // were never produced by the backend -- root-caused 2026-09-11, see workflow-types.ts's
  // RemediationContent docstring). Falls back to the in-flight `draft` so this reads live, same as
  // BuildView's StageCard, rather than staying blank until the stage's own (gateless) auto-approve.
  const remediationStage = state.stages?.remediation;
  const remediation = (remediationStage?.approved_content ?? remediationStage?.draft) as
    | RemediationContent
    | null
    | undefined;
  const complianceStage = state.stages?.["adversarial-compliance"];
  const compliance = (complianceStage?.approved_content ?? complianceStage?.draft) as
    | AdversarialComplianceReport
    | null
    | undefined;

  // Same "time lag with nothing shown" gap Build tab already surfaces via RebuildConnector (user
  // feedback 2026-09-06) applies here too: a rebuild check runs after remediation and again after
  // adversarial-compliance, unattributed to either real stage either side of it.
  const runEvents = useRunEvents();
  const [runActivity] = useRunActivity();
  const runningPhases = useMemo(
    () => computeRunningPhases(runEvents, runActivity?.runActive ?? null),
    [runEvents, runActivity?.runActive],
  );
  const rRemediation = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === "remediation");
  const rRemediationPhase = rRemediation && rebuildPhase(state, rRemediation, runActivity?.runActive, runningPhases);
  const rCompliance = REBUILD_PLACEMENTS.find((p) => p.afterStageKey === "adversarial-compliance");
  const rCompliancePhase = rCompliance && rebuildPhase(state, rCompliance, runActivity?.runActive, runningPhases);

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Quality</h1>
        <p className="text-sm text-neutral-500">
          Scan findings, remediation decisions, tests, and metrics as the pipeline hardens the repo.
        </p>
      </div>

      {(latestHealth != null || baselineHealth != null) && (
        <Section title="Health score">
          {scan?.latest_summary?.health_subscores || scan?.baseline_summary?.health_subscores ? (
            // v2: ring + the accessible per-subscore breakdown (this section, not a tooltip, is
            // the one place the weights and unmeasured legs are actually readable). Before the
            // first latest scan lands, what's on screen is the pre-build BASELINE -- say so.
            <HealthBreakdown
              summary={(scan.latest_summary?.health_subscores ? scan.latest_summary : scan.baseline_summary)!}
              baseline={scan.latest_summary?.health_subscores ? scan.baseline_summary : null}
              label={scan.latest_summary?.health_subscores ? "Health" : "Baseline health"}
            />
          ) : (
            <p className="text-sm text-neutral-700">
              Baseline {baselineHealth}
              {latestHealth != null && baselineHealth != null && (
                <>
                  {" → "}latest <span className={latestHealth >= baselineHealth ? "text-emerald-600" : "text-red-600"}>{latestHealth}</span>
                </>
              )}
            </p>
          )}
        </Section>
      )}

      {remediation && (
        <Section
          title="Remediation"
          status={
            <span className="text-xs text-neutral-500">
              {STATUS_LABEL[remediationStage?.status ?? "not_started"] ?? remediationStage?.status}
            </span>
          }
        >
          <p className="text-xs text-neutral-700">{remediation.remediation_summary}</p>
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <h3 className="mb-1 text-xs font-medium text-neutral-500">Findings addressed</h3>
              <PresenceLines items={remediation.findings_addressed} />
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-neutral-500">Dependencies upgraded</h3>
              <PresenceLines items={remediation.dependencies_upgraded} />
            </div>
            <div>
              <h3 className="mb-1 text-xs font-medium text-neutral-500">Known gaps</h3>
              <PresenceLines items={remediation.known_gaps} />
            </div>
          </div>
        </Section>
      )}
      {rRemediation && rRemediationPhase && (
        <RebuildConnector
          running={rRemediationPhase.running}
          status={rRemediationPhase.status}
          failedHere={state.run_failure?.stage === rRemediation.rebuildKey}
          isRedGate={false}
        />
      )}

      {compliance && (
        <Section title="Adversarial Compliance" status={<span className={`text-xs font-medium ${VERDICT_CLASS[compliance.overall_verdict] ?? "text-neutral-500"}`}>{compliance.overall_verdict.replace(/_/g, " ")}</span>}>
          <p className="text-xs text-neutral-700">{compliance.plan_conformance_summary}</p>
          <div>
            <h3 className="mb-1 text-xs font-medium text-neutral-500">Divergence findings</h3>
            {compliance.divergence_findings.status === "absent" ? (
              <p className="text-xs italic text-neutral-500">{compliance.divergence_findings.reason || "None."}</p>
            ) : (
              <ul className="space-y-1 text-xs text-neutral-700">
                {compliance.divergence_findings.values.map((d) => (
                  <li key={d.id}>
                    <span className={SEVERITY_CLASS[d.severity] ?? ""}>{d.severity}</span> — {d.plan_reference}: {d.description}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div>
            <h3 className="mb-1 text-xs font-medium text-neutral-500">Unresolved risk notes</h3>
            <PresenceLines items={compliance.unresolved_risk_notes} />
          </div>
        </Section>
      )}
      {rCompliance && rCompliancePhase && (
        <RebuildConnector
          running={rCompliancePhase.running}
          status={rCompliancePhase.status}
          failedHere={state.run_failure?.stage === rCompliance.rebuildKey}
          isRedGate={false}
        />
      )}

      {tests && (
        <Section title="Test hardening">
          <div className="space-y-1 text-xs text-neutral-700">
            <p>Stable failures: {(tests.stable_fail ?? []).length === 0 ? "none" : (tests.stable_fail ?? []).join(", ")}</p>
            <p>Flaky (quarantined): {(tests.flaky ?? []).length === 0 ? "none" : (tests.flaky ?? []).join(", ")}</p>
          </div>
        </Section>
      )}

      {metrics && (
        <Section title="Final metrics">
          <div className="space-y-1 text-xs text-neutral-700">
            {metrics.coverage?.line_rate != null && <p>Line coverage: {metrics.coverage.line_rate.toFixed(1)}%</p>}
            {metrics.coverage?.branch_rate != null && <p>Branch coverage: {metrics.coverage.branch_rate.toFixed(1)}%</p>}
            {metrics.traceability_summary && (
              <p>
                Traceability: {metrics.traceability_summary.covered}/{metrics.traceability_summary.total} covered,{" "}
                {metrics.traceability_summary.untested} untested
              </p>
            )}
          </div>
        </Section>
      )}

      {/* Mid-run reattach gap (same fold-in fix as SessionOverview/BuildView, 2026-09-11):
          state.stages is empty for the whole gap, which used to leave this permanently on "haven't
          run yet" even once the durable current_stage confirmed Quality was already running --
          user-reported live, thread 8242ea6d (reattached at Remediation, tab still said not run).
          Root-caused 2026-09-12: this ALSO used to claim "running" forever after, since it only
          ever compared stage order and never checked whether the run had actually stopped/failed
          in the meantime -- same class of staleness qualityDot (AppShell.tsx) was already fixed
          for; this leftover paragraph never got the same treatment. */}
      {!remediation && !compliance && !tests && !metrics && (
        <p className="text-sm text-neutral-500">
          {stageOrderIndex(runActivity?.currentStage) < stageOrderIndex("remediation")
            ? "Quality stages haven’t run yet — they start after the build stages complete."
            : runActivity?.status === "failed"
              ? "This run stopped before Quality's detail synced — see the Overview tab for the failure and Resume."
              : runActivity?.interrupted
                ? "This run appears to have stopped — see the Overview tab to Resume."
                : "Quality stages are running — details will appear here as they sync."}
        </p>
      )}
    </ViewContainer>
  );
}
