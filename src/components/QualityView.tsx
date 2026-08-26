"use client";

import { useAgent } from "@copilotkit/react-core/v2";
import { HealthBreakdown } from "@/components/HealthRing";
import { ViewContainer } from "@/components/ViewContainer";
import { useWorkflowThread } from "@/lib/workflow-thread-context";
import type { RemediationFinding, WorkflowState } from "@/lib/workflow-types";

const SEVERITY_CLASS: Record<string, string> = {
  critical: "text-red-700 font-semibold",
  high: "text-red-600",
  error: "text-red-600",
  medium: "text-amber-600",
  warning: "text-amber-600",
  low: "text-neutral-500",
  info: "text-neutral-400",
};

function FindingsTable({ findings, decisions }: { findings: RemediationFinding[]; decisions?: Record<string, { decision: string }> }) {
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

function Flag({ ok, okLabel, badLabel }: { ok: boolean | null | undefined; okLabel: string; badLabel: string }) {
  if (ok == null) return null;
  return (
    <span className={`text-xs ${ok ? "text-emerald-600" : "text-red-600"}`}>{ok ? okLabel : badLabel}</span>
  );
}

export function QualityView() {
  // agentId only -- AppShell already registered the proxied agent (see RequirementsView.tsx).
  const { localAgentId } = useWorkflowThread();
  const { agent } = useAgent({ agentId: localAgentId });
  const state = (agent.state ?? {}) as WorkflowState;
  const quality = state.quality_remediation;
  const security = state.security_remediation;
  const tests = state.test_hardening;
  const metrics = state.metrics_report?.metrics;
  const scan = state.repo_scan;

  const baselineHealth = scan?.baseline_summary?.health_score;
  const latestHealth = scan?.latest_summary?.health_score;

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
            // the one place the weights and unmeasured legs are actually readable).
            <HealthBreakdown
              summary={(scan.latest_summary?.health_subscores ? scan.latest_summary : scan.baseline_summary)!}
              baseline={scan.latest_summary?.health_subscores ? scan.baseline_summary : null}
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

      {quality && (
        <Section
          title="Code quality"
          status={
            <span className="flex gap-3">
              <Flag ok={quality.build_ok} okLabel="build ok" badLabel="build failing" />
              {quality.duplication_percent != null && (
                <span className="text-xs text-neutral-500">duplication {quality.duplication_percent}%</span>
              )}
            </span>
          }
        >
          <FindingsTable findings={quality.findings ?? []} decisions={quality.decisions} />
        </Section>
      )}

      {security && (
        <Section
          title="Security"
          status={
            <span className="flex gap-3">
              <Flag ok={security.sbom_ok} okLabel="SBOM ok" badLabel="SBOM failed" />
              {security.last_gate_report && (
                <Flag ok={security.last_gate_report.passed} okLabel="gate passed" badLabel="gate failing" />
              )}
            </span>
          }
        >
          <FindingsTable findings={security.findings ?? []} decisions={security.decisions} />
        </Section>
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

      {!quality && !security && !tests && !metrics && (
        <p className="text-sm text-neutral-500">Quality stages haven’t run yet — they start after the build stages complete.</p>
      )}
    </ViewContainer>
  );
}
