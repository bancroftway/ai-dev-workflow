import ReactMarkdown from "react-markdown";
import { Chip, type MetricThresholds } from "@/components/MetricsBar";
import { ViewContainer } from "@/components/ViewContainer";
import {
  GRADE_TONE,
  gradeHigherIsBetter,
  gradeLowerIsBetter,
  securityGrade,
  securityOpenCount,
} from "@/lib/metric-grades";
import type { DeltaSummary, MergeReadinessReport, MetricsReportState, ScanMeasures } from "@/lib/workflow-types";

export interface FilesChangedSummary {
  stat?: string;
  commits?: string;
}

export interface ReportViewProps {
  report?: MergeReadinessReport | null;
  metrics?: MetricsReportState["metrics"];
  deltaSummary?: DeltaSummary | null;
  filesChanged?: FilesChangedSummary | null;
  /** Already-resolved <img> src URLs (through the raw-content proxy) -- never raw repo paths. */
  screenshotUrls?: string[];
  thresholds: MetricThresholds;
}

/**
 * Presentational exit-report view, shared by AppShell's live Report tab and the past-session
 * report page (src/app/sessions/[owner]/[repo]/[runId]/report/page.tsx) -- identical rendering
 * whether the data came from live agent state or a committed history/<run_id>-report.json.
 *
 * SECURITY: `pr_description_markdown` is repo-controlled (an LLM's own PR description, ultimately
 * derived from an approved Specification/Plan a human already reviewed) but still untrusted HTML
 * surface -- ReactMarkdown here uses its DEFAULT url sanitizer (no urlTransform override, unlike
 * RequirementsView's attachment-preview case) and no rehype-raw, so no raw HTML/script can render.
 */
export function ReportView({ report, metrics, deltaSummary, filesChanged, screenshotUrls, thresholds }: ReportViewProps) {
  const measures = metrics?.repo_scan?.summary?.measures;
  const gatingCount = metrics?.repo_scan?.summary?.gating_count;

  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Report</h1>
        <p className="text-sm text-neutral-500">Merge readiness, metrics, and what this run produced.</p>
      </div>

      <MergeReadyBanner report={report} />

      {report ? (
        <>
          {report.blocking_reasons.length > 0 && (
            <div className="space-y-1 rounded-lg border border-red-300 bg-red-50 p-4">
              <h2 className="text-sm font-medium text-red-900">Blocking reasons</h2>
              <ul className="list-disc space-y-1 pl-5 text-sm text-red-900">
                {report.blocking_reasons.map((reason, i) => (
                  <li key={i}>{reason}</li>
                ))}
              </ul>
            </div>
          )}

          <div>
            <h2 className="text-base font-semibold">{report.pr_title || "(no title recorded)"}</h2>
            <div className="prose prose-sm mt-2 max-w-none">
              <ReactMarkdown>{report.pr_description_markdown || "Not recorded for this run."}</ReactMarkdown>
            </div>
          </div>

          {report.risk_notes.length > 0 && (
            <div className="space-y-1 rounded-lg border border-amber-300 bg-amber-50 p-4">
              <h2 className="text-sm font-medium text-amber-900">Risk notes</h2>
              <ul className="list-disc space-y-1 pl-5 text-sm text-amber-900">
                {report.risk_notes.map((note, i) => (
                  <li key={i}>{note}</li>
                ))}
              </ul>
            </div>
          )}

          {report.suggested_reviewers_note && (
            <p className="text-xs text-neutral-500">Reviewer note: {report.suggested_reviewers_note}</p>
          )}
        </>
      ) : (
        <p className="text-sm text-neutral-400">Not recorded for this run.</p>
      )}

      <div>
        <h2 className="mb-2 text-sm font-medium text-neutral-700">Metrics</h2>
        <div className="flex flex-wrap items-center gap-2">
          {measures ? (
            <MetricChips measures={measures} gatingCount={gatingCount} thresholds={thresholds} />
          ) : (
            <span className="text-sm text-neutral-400">Not recorded for this run.</span>
          )}
        </div>
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-neutral-700">Delta vs baseline</h2>
        <DeltaTable deltaSummary={deltaSummary} />
      </div>

      <div>
        <h2 className="mb-2 text-sm font-medium text-neutral-700">What was produced</h2>
        {filesChanged?.stat || filesChanged?.commits ? (
          <div className="space-y-2">
            {filesChanged.stat && (
              <pre className="max-h-64 overflow-auto rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-xs">{filesChanged.stat}</pre>
            )}
            {filesChanged.commits && (
              <pre className="max-h-64 overflow-auto rounded-lg border border-neutral-200 bg-neutral-50 p-3 text-xs">{filesChanged.commits}</pre>
            )}
          </div>
        ) : (
          <p className="text-sm text-neutral-400">Not recorded for this run.</p>
        )}
      </div>

      {screenshotUrls && screenshotUrls.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-neutral-700">E2E Screenshots</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {screenshotUrls.map((url) => (
              <img key={url} src={url} loading="lazy" className="max-w-full rounded-md border border-neutral-200" alt="E2E screenshot" />
            ))}
          </div>
        </div>
      )}
    </ViewContainer>
  );
}

function MergeReadyBanner({ report }: { report?: MergeReadinessReport | null }) {
  if (!report) {
    return (
      <div className="rounded-lg border border-neutral-300 bg-neutral-50 px-4 py-3 text-sm text-neutral-600">
        Merge readiness not recorded for this run.
      </div>
    );
  }
  const ready = report.merge_ready;
  return (
    <div
      className={`rounded-lg border px-4 py-3 text-sm font-medium ${
        ready ? "border-emerald-300 bg-emerald-50 text-emerald-900" : "border-red-300 bg-red-50 text-red-900"
      }`}
    >
      {ready ? "Ready to merge" : "Not ready to merge"}
    </div>
  );
}

function MetricChips({
  measures,
  gatingCount,
  thresholds,
}: {
  measures: ScanMeasures;
  gatingCount: number | undefined;
  thresholds: MetricThresholds;
}) {
  const worst = measures.security.worst_open_severity;
  const secGrade = securityGrade(worst);
  const secCount = securityOpenCount(measures.security.by_severity);

  const ccn = measures.mean_ccn;
  const ccnGrade = ccn != null ? gradeLowerIsBetter(ccn, thresholds.ccn) : null;

  const coverage = measures.coverage_line_rate;
  const coverageGrade = coverage != null ? gradeHigherIsBetter(coverage, thresholds.coverage) : null;

  const dup = measures.duplication_percent;
  const dupGrade = dup != null ? gradeLowerIsBetter(dup, thresholds.dup) : null;

  return (
    <>
      <Chip label="Security" value={`${secGrade} · ${secCount}`} tone={GRADE_TONE[secGrade]} />
      <Chip
        label="Maintainability"
        value={ccn != null && ccnGrade ? `${ccnGrade} · ${ccn.toFixed(1)}` : "—"}
        tone={ccnGrade ? GRADE_TONE[ccnGrade] : "gray"}
      />
      <Chip
        label="Coverage"
        value={coverage != null && coverageGrade ? `${coverageGrade} · ${coverage.toFixed(0)}%` : "—"}
        tone={coverageGrade ? GRADE_TONE[coverageGrade] : "gray"}
      />
      <Chip
        label="Duplication"
        value={dup != null && dupGrade ? `${dupGrade} · ${dup.toFixed(1)}%` : "—"}
        tone={dupGrade ? GRADE_TONE[dupGrade] : "gray"}
      />
      {gatingCount != null && (
        <Chip label="Gate" value={gatingCount === 0 ? "Pass" : `Fail · ${gatingCount}`} tone={gatingCount === 0 ? "green" : "red"} />
      )}
    </>
  );
}

function DeltaTable({ deltaSummary }: { deltaSummary?: DeltaSummary | null }) {
  if (!deltaSummary) {
    return <p className="text-sm text-neutral-400">No baseline recorded for this repository -- nothing to diff.</p>;
  }
  const entries = Object.entries(deltaSummary.metrics || {});
  return (
    <div className="space-y-2">
      {entries.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-neutral-200">
          <table className="w-full text-left text-sm">
            <thead className="bg-neutral-50 text-xs uppercase text-neutral-500">
              <tr>
                <th className="px-3 py-2">Metric</th>
                <th className="px-3 py-2">Before</th>
                <th className="px-3 py-2">After</th>
                <th className="px-3 py-2">Change</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([name, d]) => (
                <tr key={name} className="border-t border-neutral-100">
                  <td className="px-3 py-2 font-medium">{name}</td>
                  <td className="px-3 py-2">{d.from}</td>
                  <td className="px-3 py-2">{d.to}</td>
                  <td className={`px-3 py-2 ${d.direction === "improved" ? "text-emerald-700" : d.direction === "regressed" ? "text-red-700" : "text-neutral-500"}`}>
                    {d.delta} ({d.direction})
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-xs text-neutral-500">
        Findings: {deltaSummary.fixed_count} fixed, {deltaSummary.introduced_count} introduced, {deltaSummary.severity_changed} severity-changed.
      </p>
    </div>
  );
}
