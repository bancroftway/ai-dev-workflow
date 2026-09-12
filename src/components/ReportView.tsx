import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { FindingsTable } from "@/components/QualityView";
import { ViewContainer } from "@/components/ViewContainer";
import type { DeltaSummary, MergeReadinessReport, RemediationFinding } from "@/lib/workflow-types";

export interface FilesChangedSummary {
  stat?: string;
  commits?: string;
}

/** exit_nodes.py's `_stage_summary()` row shape -- per-stage runtime/laps/tokens/cost, already
 * committed to report.json's `stage_summary`, previously only visible in the markdown copy. */
export interface StageSummaryRow {
  stage: string;
  status: string;
  runtime_seconds: number;
  laps: number;
  input_tokens: number;
  output_tokens: number;
  cost: number | null;
  notes: string[];
}

/** exit_nodes.py's `_us_ac_rows()` row shape -- report.json's `us_ac`. Covers both user_story and
 * acceptance_criterion rows; the Report tab only renders the AC rows. */
export interface UsAcRow {
  id: string;
  kind: "user_story" | "acceptance_criterion";
  title_or_description: string;
  change: string;
  coded_run_id: string | null;
  tested_run_id: string | null;
  test_ids: string[];
}

/** ac_eval.py's `execution_summary()` per-AC entry -- report.json's `metrics.ac_execution.per_ac`,
 * keyed by AC id. Richer than `test_ids.length > 0`: this is a real pass/fail/flaky verdict from
 * actually running the suite, not just "a test exists". */
export interface AcExecutionEntry {
  runs: number;
  passed: number;
  flaky: boolean;
  status: "pass" | "fail" | "not_run";
  test_names?: string[];
}

/** The slice of report.json this view surfaces beyond files_changed/commits -- all already
 * computed and committed by exit_finalize_node, just unread by the frontend until now. */
export interface ReportExtras {
  stageSummary?: StageSummaryRow[];
  usAc?: UsAcRow[];
  findings?: RemediationFinding[];
  acExecutionPerAc?: Record<string, AcExecutionEntry>;
}

export interface ReportViewProps {
  report?: MergeReadinessReport | null;
  /** state.stages["metrics-exit"]?.status -- lets the merge-readiness banner say "still running"
   * instead of a permanent-looking "not recorded" while the run just hasn't gotten there yet. */
  metricsExitStatus?: string;
  deltaSummary?: DeltaSummary | null;
  filesChanged?: FilesChangedSummary | null;
  /** Already-resolved <img> src URLs (through the raw-content proxy) -- never raw repo paths. */
  screenshotUrls?: string[];
  reportExtras?: ReportExtras | null;
}

/**
 * Presentational exit-report view -- AppShell's Report tab, the one place this renders (the
 * standalone past-session route this used to also serve is gone; a completed session's own
 * committed report.json now only supplies what live state can't -- filesChanged and the
 * findings/AC/stage-summary detail in `reportExtras` -- which the workflow page reads
 * server-side and passes through AppShell).
 *
 * SECURITY: `pr_description_markdown` is repo-controlled (an LLM's own PR description, ultimately
 * derived from an approved Specification/Plan a human already reviewed) but still untrusted HTML
 * surface -- ReactMarkdown here uses its DEFAULT url sanitizer (no urlTransform override, unlike
 * RequirementsView's attachment-preview case) and no rehype-raw, so no raw HTML/script can render.
 */
export function ReportView({ report, metricsExitStatus, deltaSummary, filesChanged, screenshotUrls, reportExtras }: ReportViewProps) {
  return (
    <ViewContainer>
      <div>
        <h1 className="text-lg font-semibold">Report</h1>
        <p className="text-sm text-neutral-500">Merge readiness, metrics, and what this run produced.</p>
      </div>

      <MergeReadyBanner report={report} metricsExitStatus={metricsExitStatus} />

      {report ? (
        <>
          {report.blocking_reasons.status === "present" && (
            <div className="space-y-1 rounded-lg border border-red-300 bg-red-50 p-4">
              <h2 className="text-sm font-medium text-red-900">Blocking reasons</h2>
              <ul className="list-disc space-y-1 pl-5 text-sm text-red-900">
                {report.blocking_reasons.values.map((reason, i) => (
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

          {report.risk_notes.status === "present" && (
            <div className="space-y-1 rounded-lg border border-amber-300 bg-amber-50 p-4">
              <h2 className="text-sm font-medium text-amber-900">Risk notes</h2>
              <ul className="list-disc space-y-1 pl-5 text-sm text-amber-900">
                {report.risk_notes.values.map((note, i) => (
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
        <h2 className="mb-2 text-sm font-medium text-neutral-700">Delta vs baseline</h2>
        <DeltaTable deltaSummary={deltaSummary} />
      </div>

      {reportExtras?.findings && reportExtras.findings.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-neutral-700">Findings</h2>
          <FindingsTable findings={reportExtras.findings} />
        </div>
      )}

      {reportExtras?.usAc && reportExtras.usAc.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-neutral-700">Acceptance criteria coverage</h2>
          <AcCoverageTable rows={reportExtras.usAc} perAc={reportExtras.acExecutionPerAc} />
        </div>
      )}

      {reportExtras?.stageSummary && reportExtras.stageSummary.length > 0 && (
        <div>
          <h2 className="mb-2 text-sm font-medium text-neutral-700">Stage summary</h2>
          <StageSummaryTable rows={reportExtras.stageSummary} />
        </div>
      )}

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
          <ScreenshotGrid urls={screenshotUrls} />
        </div>
      )}
    </ViewContainer>
  );
}

function MergeReadyBanner({ report, metricsExitStatus }: { report?: MergeReadinessReport | null; metricsExitStatus?: string }) {
  if (!report) {
    // The Report tab unlocks as soon as state.metrics_report?.metrics exists, which is EARLIER
    // than the metrics-exit stage's own approval (the only thing that ever populates `report`) --
    // so "not recorded" here is usually just "hasn't gotten there yet", not a real gap. Say so.
    const stillRunning = metricsExitStatus != null && metricsExitStatus !== "approved";
    return (
      <div className="rounded-lg border border-neutral-300 bg-neutral-50 px-4 py-3 text-sm text-neutral-600">
        {stillRunning
          ? "Still running — merge readiness is judged at the end of the run."
          : "Merge readiness not recorded for this run."}
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

/** Click-to-zoom screenshots: a native <dialog> (no lightbox library) gives Esc-to-close and, via
 * the backdrop-click check below, click-outside-to-close for free. */
function ScreenshotGrid({ urls }: { urls: string[] }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [active, setActive] = useState<string | null>(null);

  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {urls.map((url) => (
          <button
            key={url}
            type="button"
            onClick={() => {
              setActive(url);
              dialogRef.current?.showModal();
            }}
            className="cursor-zoom-in"
            aria-label="View screenshot full size"
          >
            <img src={url} loading="lazy" className="max-w-full rounded-md border border-neutral-200" alt="E2E screenshot" />
          </button>
        ))}
      </div>
      <dialog
        ref={dialogRef}
        className="max-h-[90vh] max-w-[90vw] rounded-lg bg-transparent p-0 backdrop:bg-black/70"
        onClick={(e) => {
          // Clicking the <dialog> element itself (not a descendant) means the click landed on the
          // backdrop area -- the standard native-<dialog> "click outside to close" pattern.
          if (e.target === dialogRef.current) dialogRef.current?.close();
        }}
      >
        {active && <img src={active} className="max-h-[90vh] max-w-[90vw] rounded-lg" alt="E2E screenshot, full size" />}
      </dialog>
    </>
  );
}

function AcCoverageTable({ rows, perAc }: { rows: UsAcRow[]; perAc?: Record<string, AcExecutionEntry> }) {
  const acRows = rows.filter((r) => r.kind === "acceptance_criterion");
  if (acRows.length === 0) {
    return <p className="text-sm text-neutral-400">No acceptance criteria recorded for this run.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-50 text-xs uppercase text-neutral-500">
          <tr>
            <th className="px-3 py-2">AC</th>
            <th className="px-3 py-2">Description</th>
            <th className="px-3 py-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {acRows.map((r) => {
            const exec = perAc?.[r.id];
            // Prefer a real execution verdict (actually ran the suite) over the static "a test
            // exists" fallback -- richer and honest about flaky/failing, not just linked/unlinked.
            const status = exec
              ? exec.flaky
                ? "flaky"
                : exec.status === "pass"
                  ? "solid"
                  : exec.status === "fail"
                    ? "failing"
                    : "not run"
              : r.test_ids.length > 0
                ? "has test (unexecuted)"
                : "not run";
            const tone =
              status === "solid"
                ? "text-emerald-700"
                : status === "flaky"
                  ? "text-amber-600"
                  : status === "failing"
                    ? "text-red-700"
                    : "text-neutral-500";
            return (
              <tr key={r.id} className="border-t border-neutral-100 align-top">
                <td className="px-3 py-2 font-mono">{r.id}</td>
                <td className="px-3 py-2 text-neutral-700">{r.title_or_description}</td>
                <td className={`px-3 py-2 ${tone}`}>{status}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function StageSummaryTable({ rows }: { rows: StageSummaryRow[] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-50 text-xs uppercase text-neutral-500">
          <tr>
            <th className="px-3 py-2">Stage</th>
            <th className="px-3 py-2">Status</th>
            <th className="px-3 py-2">Runtime</th>
            <th className="px-3 py-2">Laps</th>
            <th className="px-3 py-2">Tokens</th>
            <th className="px-3 py-2">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.stage} className="border-t border-neutral-100 align-top">
              <td className="px-3 py-2 font-medium">{r.stage}</td>
              <td className="px-3 py-2">{r.status}</td>
              <td className="px-3 py-2">{r.runtime_seconds.toFixed(0)}s</td>
              <td className="px-3 py-2">{r.laps}</td>
              <td className="px-3 py-2">{(r.input_tokens + r.output_tokens).toLocaleString()}</td>
              <td className="px-3 py-2">{r.cost != null ? `$${r.cost.toFixed(4)}` : "n/a"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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
