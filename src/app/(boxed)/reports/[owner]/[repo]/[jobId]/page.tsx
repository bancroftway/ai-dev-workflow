"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { Chip } from "@/components/MetricsBar";
import { HealthBreakdown } from "@/components/HealthRing";
import { HotspotsTable } from "@/components/HotspotsTable";
import { FindingsTable } from "@/components/QualityView";
import {
  GRADE_TONE,
  gradeLowerIsBetter,
  securityGrade,
  securityOpenCount,
  type Thresholds4,
} from "@/lib/metric-grades";
import { findingsToSarif } from "@/lib/sarif-export";
import type { HotspotEntry, RemediationFinding, ScanSummary, ScanTool } from "@/lib/workflow-types";

// Same defaults the live workflow page passes MetricsBar (src/app/workflow/.../page.tsx) --
// kept as plain literals here rather than threaded through env, since this is a client
// component with no server-side env read, and this report has no per-repo override need.
const CCN_THRESHOLDS: Thresholds4 = [5, 10, 15, 20];
const DUP_THRESHOLDS: Thresholds4 = [3, 5, 10, 20];

/** Renders a lower-is-better Thresholds4 as its actual band cutoffs, e.g. "A ≤5 · B ≤10 ..." --
 * generated from the same array the chip is graded against, so the explanation can never drift
 * out of sync with the grading logic the way a hand-typed copy of the numbers could. */
function describeBands(thresholds: Thresholds4, unit: string): string {
  const [a, b, c, d] = thresholds;
  return `A ≤${a}${unit} · B ≤${b}${unit} · C ≤${c}${unit} · D ≤${d}${unit} · E >${d}${unit}`;
}

function matchesFilter(fields: (string | number | null | undefined)[], query: string): boolean {
  if (!query.trim()) return true;
  const q = query.trim().toLowerCase();
  return fields.some((f) => f != null && String(f).toLowerCase().includes(q));
}

const TOOL_STATUS_CLASS: Record<string, string> = {
  ok: "text-emerald-700",
  failed: "text-red-600",
  missing: "text-red-600",
  not_applicable: "text-neutral-400",
};

// repo_scan.py's SECURITY_GRADING_CATEGORIES (SECURITY_CATEGORIES minus "license", which this
// report's light profile never produces anyway -- no trivy/checkov/osv-scanner). Filters
// report.findings down to the ones the Security chip's grade/count are actually derived from.
const SECURITY_FINDING_CATEGORIES = new Set(["sast", "secret", "vulnerability", "misconfig"]);
// lizard's own per-function over-threshold finding (repo_scan.py:397) -- what the Complexity
// section's table below is built from, via metrics.complexity.worst instead (same data,
// pre-sorted, with clean numeric fields rather than a formatted message string).
const COMPLEXITY_RULE_ID = "high-cyclomatic-complexity";

interface SbomComponent {
  name?: string;
  version?: string;
  type?: string;
  purl?: string;
}

interface DuplicationClone {
  path: string;
  start_line: number | null;
  duplicate_of: string;
  lines: number | null;
}

interface ComplexityWorstFn {
  path: string;
  function: string;
  ccn: number;
  nloc: number;
}

interface HealthReportPayload {
  schema_version?: number;
  generated_at?: string;
  content_hash?: string;
  summary?: ScanSummary;
  findings?: RemediationFinding[];
  tools?: ScanTool[];
  metrics?: {
    churn?: { hotspots?: HotspotEntry[] };
    duplication?: { percent?: number; clone_count?: number; threshold?: number; clones?: DuplicationClone[] };
    complexity?: { mean_ccn?: number; functions_total?: number; functions_over_threshold?: number; threshold?: number; worst?: ComplexityWorstFn[] };
  };
  sbom?: { components?: SbomComponent[]; specVersion?: string } | null;
  // repo_scan.py's ScanReport.repo (_repo_facts): the exact commit this scan ran against, plus
  // the branch it was checked out on. `commit` is what GitHub links below are built from -- a
  // branch ref can move after the scan runs, a commit SHA can't, so linking to the branch would
  // eventually point at different code than what was actually scanned.
  repo?: { commit?: string; branch?: string };
}

/** A direct GitHub link to the exact line(s) a finding/clone/function is at, so clicking it opens
 * that file on GitHub with the line(s) highlighted (GitHub's own `#L{n}` / `#L{a}-L{b}` fragment
 * convention) -- no path-only link ever left unclickable just because a line number happens to
 * be missing (jscpd's "duplicate of" side and lizard's per-function entries don't carry one; see
 * ComplexityTable/DuplicationTable). Pinned to the scanned commit (falls back to branch only if
 * the scan somehow didn't record one) so the link never drifts to different code than what was
 * actually measured. */
function githubFileUrl(owner: string, repo: string, ref: string, path: string, startLine?: number | null, endLine?: number | null): string {
  const encodedPath = path
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  const fragment = startLine != null ? (endLine != null && endLine !== startLine ? `#L${startLine}-L${endLine}` : `#L${startLine}`) : "";
  return `https://github.com/${owner}/${repo}/blob/${encodeURIComponent(ref)}/${encodedPath}${fragment}`;
}

interface HealthReportJob {
  job_id: string;
  owner: string;
  repo: string;
  branch: string;
  status: "queued" | "running" | "completed" | "failed";
  report: HealthReportPayload | null;
  error: string | null;
}

function download(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Every value below ultimately comes from scanner output (finding titles/messages, rule ids,
 * file paths) or GitHub owner/repo names -- none of it is safe to interpolate into HTML
 * unescaped. A crafted finding message (or a repo/branch name) could otherwise inject a live
 * `<script>` into this exported, standalone file. */
function esc(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Everything the live page derives from `report` that this builder also needs -- passed in
 * rather than recomputed here, so the downloaded file is GUARANTEED to match what's on screen
 * (one filtering/splitting implementation, not two that can drift apart). */
interface StandaloneHtmlData {
  securityFindings: RemediationFinding[];
  otherFindings: RemediationFinding[];
  duplicationClones: DuplicationClone[];
  complexityWorst: ComplexityWorstFn[];
  hotspots: HotspotEntry[];
  sbomComponents: SbomComponent[];
  gitRef?: string;
}

function fileLinkHtml(owner: string, repo: string, gitRef: string | undefined, path: string, startLine?: number | null, endLine?: number | null): string {
  if (!path) return "";
  const text = esc(`${path}${startLine != null ? `:${startLine}${endLine != null && endLine !== startLine ? `-${endLine}` : ""}` : ""}`);
  if (!gitRef) return `<span class="mono">${text}</span>`;
  return `<a class="mono" href="${esc(githubFileUrl(owner, repo, gitRef, path, startLine, endLine))}" target="_blank" rel="noreferrer">${text}</a>`;
}

function findingsTableHtml(owner: string, repo: string, gitRef: string | undefined, findings: RemediationFinding[]): string {
  if (findings.length === 0) return `<p class="muted">No findings.</p>`;
  const rows = findings
    .map((f) => {
      const path = f.location?.path;
      return `<tr><td>${esc(f.severity ?? "")}</td><td>${esc(f.category ?? "")}</td><td>${esc((f.tools ?? []).join(", "))}</td><td>${
        esc(f.rule_id ?? "")
      }</td><td>${esc(f.title ?? f.description ?? "")}</td><td>${path ? fileLinkHtml(owner, repo, gitRef, path, f.location?.start_line, f.location?.end_line) : "—"}</td></tr>`;
    })
    .join("");
  return `<table><thead><tr><th>Severity</th><th>Category</th><th>Tools</th><th>Rule</th><th>Title</th><th>Location</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function buildStandaloneHtml(job: HealthReportJob, report: HealthReportPayload, data: StandaloneHtmlData): string {
  const summary = report.summary;
  const measures = summary?.measures;
  const { owner, repo } = job;
  const gitRef = data.gitRef;

  const toolRows = (report.tools ?? [])
    .map(
      (t) =>
        `<tr><td>${esc(t.name)}</td><td>${esc(t.status)}</td><td>${esc(t.version ?? "—")}</td><td>${
          t.duration_ms != null ? esc(`${(t.duration_ms / 1000).toFixed(1)}s`) : "—"
        }</td><td>${esc(t.findings ?? 0)}</td><td>${esc(t.notes || "—")}</td></tr>`,
    )
    .join("");

  const scoreRows = Object.entries(summary?.health_subscores ?? {})
    .filter(([, v]) => v != null)
    .map(
      ([k, v]) =>
        `<tr><td>${esc(k)}</td><td>${esc(v)}</td><td>${
          summary?.health_weights_used?.[k] != null ? esc(`${Math.round((summary!.health_weights_used![k] ?? 0) * 100)}%`) : "—"
        }</td><td>${esc(summary?.health_basis?.[k] ?? "")}</td></tr>`,
    )
    .join("");

  const hotspotRows = data.hotspots
    .map((h) => `<tr><td>${fileLinkHtml(owner, repo, gitRef, h.path)}</td><td>${esc(h.commits)}</td><td>${esc(h.ccn)}</td><td>${esc(h.hotspot_score.toFixed(1))}</td></tr>`)
    .join("");

  const duplicationRows = data.duplicationClones
    .map(
      (c) =>
        `<tr><td>${fileLinkHtml(owner, repo, gitRef, c.path, c.start_line)}</td><td>${fileLinkHtml(owner, repo, gitRef, c.duplicate_of)}</td><td>${esc(c.lines ?? "—")}</td></tr>`,
    )
    .join("");

  const complexityRows = data.complexityWorst
    .map((w) => `<tr><td>${fileLinkHtml(owner, repo, gitRef, w.path)}</td><td>${esc(w.function)}</td><td>${esc(w.ccn)}</td><td>${esc(w.nloc)}</td></tr>`)
    .join("");

  const sbomRows = data.sbomComponents
    .map((c) => `<tr><td>${esc(c.name ?? "—")}</td><td>${esc(c.version ?? "—")}</td><td>${esc(c.type ?? "—")}</td></tr>`)
    .join("");

  const securityGradeValue = measures ? securityGrade(measures.security.worst_open_severity) : "E";
  const dupValue = measures?.duplication_percent;
  const ccnValue = measures?.mean_ccn;

  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Repository Health Report — ${esc(owner)}/${esc(repo)}</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;color:#171717;max-width:min(96vw,80rem);margin:2rem auto;padding:0 1rem;font-size:1rem;line-height:1.5}
h1{font-size:1.5rem}h2{font-size:1.125rem;margin-top:2.5rem;border-bottom:1px solid #e5e5e5;padding-bottom:.25rem}
h3{font-size:1rem;margin-bottom:.25rem}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin-top:.5rem}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #f0f0f0;vertical-align:top}
th{color:#737373;text-transform:uppercase;font-size:.75rem}
.disclaimer{background:#fffbeb;border:1px solid #fcd34d;color:#78350f;padding:.75rem 1rem;border-radius:.5rem;font-size:.9rem}
.mono{font-family:ui-monospace,monospace}
.muted{color:#a3a3a3;font-size:.9rem}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem;margin-top:1rem}
.card{border:1px solid #e5e5e5;border-radius:.5rem;padding:1rem}
.chip{display:inline-block;border-radius:999px;border:1px solid #6ee7b7;background:#ecfdf5;color:#065f46;padding:.15rem .6rem;font-size:.8rem}
a{color:#1d4ed8}
pre{white-space:pre-wrap;font-size:.7rem;max-height:24rem;overflow:auto;background:#fafafa;border:1px solid #e5e5e5;border-radius:.5rem;padding:.75rem}
</style></head><body>
<h1>Repository Health Report — ${esc(owner)}/${esc(repo)}@${esc(job.branch)}</h1>
<p class="mono muted">generated ${esc(report.generated_at ?? "")} · ${esc(report.content_hash ?? "")}</p>
<p class="disclaimer">Fast on-demand scan: test execution, coverage measurement, and DAST/dynamic
scans are intentionally skipped for turnaround time. This health score is NOT directly comparable
to a full workflow run's score — it omits coverage, dependency-freshness, AC verification,
accessibility and performance subscores entirely.</p>

<h2>Full score breakdown</h2>
<p class="muted">All 9 dimensions this platform can measure; dashes mean this fast scan doesn't measure that dimension (its weight is redistributed across the ones that ARE measured).</p>
<table><thead><tr><th>Dimension</th><th>Score</th><th>Weight</th><th>Basis</th></tr></thead><tbody>${scoreRows}</tbody></table>
<p><strong>Overall: ${esc(summary?.health_score ?? "—")} / 100</strong></p>

<div class="grid3">
  <div class="card">
    <h3>Security</h3>
    <p><span class="chip">${esc(securityGradeValue)} · ${esc(measures ? securityOpenCount(measures.security.by_severity) : 0)}</span></p>
    <p class="muted">Grade reflects the single worst OPEN finding's severity: A = none, B = info/low, C = medium, D = high, E = critical.${summary?.health_basis?.security ? ` ${esc(summary.health_basis.security)}.` : ""}</p>
    ${findingsTableHtml(owner, repo, gitRef, data.securityFindings)}
  </div>
  <div class="card">
    <h3>Duplication</h3>
    <p>${dupValue != null ? `<span class="chip">${esc(gradeLowerIsBetter(dupValue, DUP_THRESHOLDS))} · ${esc(dupValue.toFixed(1))}%</span>` : "—"}</p>
    <p class="muted">Percent of code lines duplicated elsewhere in the repo (jscpd). Bands: ${esc(describeBands(DUP_THRESHOLDS, "%"))}.${summary?.health_basis?.duplication ? ` ${esc(summary.health_basis.duplication)}.` : ""}</p>
    ${data.duplicationClones.length ? `<table><thead><tr><th>File</th><th>Duplicate of</th><th>Lines</th></tr></thead><tbody>${duplicationRows}</tbody></table>` : `<p class="muted">No duplicate blocks recorded.</p>`}
  </div>
  <div class="card">
    <h3>Complexity</h3>
    <p>${ccnValue != null ? `<span class="chip">${esc(gradeLowerIsBetter(ccnValue, CCN_THRESHOLDS))} · ${esc(ccnValue.toFixed(1))}</span>` : "—"}</p>
    <p class="muted">Average cyclomatic complexity (CCN) per function. Bands: ${esc(describeBands(CCN_THRESHOLDS, ""))}.${summary?.health_basis?.complexity ? ` ${esc(summary.health_basis.complexity)}.` : ""}</p>
    ${data.complexityWorst.length ? `<table><thead><tr><th>File</th><th>Function</th><th>CCN</th><th>Lines</th></tr></thead><tbody>${complexityRows}</tbody></table>` : `<p class="muted">No functions over the complexity threshold.</p>`}
  </div>
</div>

${
  data.hotspots.length
    ? `<h2>Hotspots</h2><p class="muted">Files that are both frequently changed and highly complex, highest risk first.</p><table><thead><tr><th>File</th><th>Commits</th><th>Max CCN</th><th>Score</th></tr></thead><tbody>${hotspotRows}</tbody></table>`
    : ""
}

<h2>Scanner coverage (${esc(report.tools?.length ?? 0)} tool(s))</h2>
<table><thead><tr><th>Tool</th><th>Status</th><th>Version</th><th>Duration</th><th>Findings</th><th>Notes</th></tr></thead><tbody>${toolRows}</tbody></table>

${
  data.otherFindings.length
    ? `<h2>Other findings (${data.otherFindings.length})</h2><p class="muted">Findings outside Security/Duplication/Complexity above.</p>${findingsTableHtml(owner, repo, gitRef, data.otherFindings)}`
    : ""
}

${
  data.sbomComponents.length
    ? `<h2>Software bill of materials (${data.sbomComponents.length} component(s)${report.sbom?.specVersion ? `, CycloneDX ${esc(report.sbom.specVersion)}` : ""})</h2><table><thead><tr><th>Name</th><th>Version</th><th>Type</th></tr></thead><tbody>${sbomRows}</tbody></table>`
    : ""
}

<h2>Raw scanner evidence</h2>
<pre>${esc(JSON.stringify(report, null, 2))}</pre>
</body></html>`;
}

function FilterInput({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="mb-2 w-full max-w-sm rounded-md border border-neutral-300 px-3 py-1.5 text-sm"
    />
  );
}

/** A file (and optional line range) rendered as a direct GitHub link when ref info is available,
 * plain text otherwise (e.g. the standalone downloaded HTML builds its own equivalent link
 * markup separately -- this component is JSX-only). */
function CodeLink({
  owner, repo, gitRef, path, startLine, endLine,
}: {
  owner?: string; repo?: string; gitRef?: string; path: string; startLine?: number | null; endLine?: number | null;
}) {
  if (!owner || !repo || !gitRef) return <span className="font-mono">{path}</span>;
  return (
    <a
      href={githubFileUrl(owner, repo, gitRef, path, startLine, endLine)}
      target="_blank"
      rel="noreferrer"
      className="font-mono text-blue-700 underline decoration-blue-300 hover:decoration-blue-600"
    >
      {path}
      {startLine != null ? `:${startLine}${endLine != null && endLine !== startLine ? `-${endLine}` : ""}` : ""}
    </a>
  );
}

function DuplicationTable({ clones, owner, repo, gitRef }: { clones: DuplicationClone[]; owner?: string; repo?: string; gitRef?: string }) {
  if (clones.length === 0) return <p className="text-sm text-neutral-400">No duplicate blocks recorded.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-50 text-neutral-500">
          <tr>
            <th className="px-3 py-1.5 font-medium">File</th>
            <th className="px-3 py-1.5 font-medium">Duplicate of</th>
            <th className="px-3 py-1.5 font-medium">Lines</th>
          </tr>
        </thead>
        <tbody>
          {clones.map((c, i) => (
            <tr key={`${c.path}-${i}`} className="border-t border-neutral-100 align-top">
              <td className="px-3 py-1.5">
                <CodeLink owner={owner} repo={repo} gitRef={gitRef} path={c.path} startLine={c.start_line} />
              </td>
              {/* jscpd's second-file side never carries a start line (only the first file's does),
                  so this one links to the file only -- still useful, just not line-anchored. */}
              <td className="px-3 py-1.5">
                <CodeLink owner={owner} repo={repo} gitRef={gitRef} path={c.duplicate_of} />
              </td>
              <td className="px-3 py-1.5">{c.lines ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ComplexityTable({ worst, owner, repo, gitRef }: { worst: ComplexityWorstFn[]; owner?: string; repo?: string; gitRef?: string }) {
  if (worst.length === 0) return <p className="text-sm text-neutral-400">No functions over the complexity threshold.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-50 text-neutral-500">
          <tr>
            <th className="px-3 py-1.5 font-medium">File</th>
            <th className="px-3 py-1.5 font-medium">Function</th>
            <th className="px-3 py-1.5 font-medium">CCN</th>
            <th className="px-3 py-1.5 font-medium">Lines</th>
          </tr>
        </thead>
        <tbody>
          {worst.map((w, i) => (
            <tr key={`${w.path}-${w.function}-${i}`} className="border-t border-neutral-100 align-top">
              {/* lizard's own CSV output (repo_scan.py's parse_lizard) doesn't capture a line
                  number per function today, only the file -- links to the file, not a highlighted
                  line, until that's added upstream. */}
              <td className="px-3 py-1.5">
                <CodeLink owner={owner} repo={repo} gitRef={gitRef} path={w.path} />
              </td>
              <td className="px-3 py-1.5 font-mono">{w.function}</td>
              <td className="px-3 py-1.5">{w.ccn}</td>
              <td className="px-3 py-1.5">{w.nloc}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** One dimension card (Security/Duplication/Complexity): a grade chip, a plain-English
 * explanation of what the number means and its full A-E range, and a detail table of exactly
 * what's behind it -- addresses "2.4 out of what?" by naming the scale every time, not just
 * showing a number. */
function DimensionCard({ title, chip, explanation, children }: { title: string; chip: React.ReactNode; explanation: string; children: React.ReactNode }) {
  return (
    <div className="flex h-[32rem] flex-col rounded-lg border border-neutral-200 p-4">
      <div className="shrink-0">
        <h2 className="text-lg font-semibold text-neutral-900">{title}</h2>
        <div className="mt-1">{chip}</div>
        <p className="mt-2 text-sm text-neutral-600">{explanation}</p>
      </div>
      <div className="mt-3 min-h-0 flex-1 space-y-3 overflow-y-auto">{children}</div>
    </div>
  );
}

export default function HealthReportPage() {
  const params = useParams<{ owner: string; repo: string; jobId: string }>();
  const owner = decodeURIComponent(params.owner);
  const repo = decodeURIComponent(params.repo);
  const jobId = decodeURIComponent(params.jobId);

  const [job, setJob] = useState<HealthReportJob | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [startedAt] = useState(() => Date.now());
  const [elapsed, setElapsed] = useState(0);
  const [securityFilter, setSecurityFilter] = useState("");
  const [findingsFilter, setFindingsFilter] = useState("");
  const [sbomFilter, setSbomFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetch(`/api/health-reports/${jobId}`)
        .then((res) => {
          if (!res.ok) throw new Error(`Failed to load report (${res.status})`);
          return res.json();
        })
        .then((data: HealthReportJob) => {
          if (!cancelled) {
            setJob(data);
            setPollError(null);
          }
        })
        .catch((err: Error) => {
          if (!cancelled) setPollError(err.message);
        });
    }
    load();
    const interval = setInterval(load, 15_000);
    window.addEventListener("focus", load);
    return () => {
      cancelled = true;
      clearInterval(interval);
      window.removeEventListener("focus", load);
    };
  }, [jobId]);

  useEffect(() => {
    if (job?.status === "completed" || job?.status === "failed") return;
    const timer = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [job?.status, startedAt]);

  if (pollError) {
    return <p className="p-6 text-base text-red-600">{pollError}</p>;
  }
  if (!job || job.status === "queued" || job.status === "running") {
    const mins = Math.floor(elapsed / 60);
    const secs = elapsed % 60;
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Generating Repository Health Report…</h1>
        <p className="mt-2 text-base text-neutral-500">
          {owner}/{repo}
          {job?.branch ? `@${job.branch}` : ""} — typically ~5 minutes. Elapsed: {mins}m {secs}s.
        </p>
      </div>
    );
  }
  if (job.status === "failed") {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold text-red-700">Report generation failed</h1>
        <p className="mt-2 text-base text-red-600">{job.error}</p>
      </div>
    );
  }

  const report = job.report;
  if (!report) {
    return <p className="p-6 text-base text-neutral-500">Report completed but produced no data.</p>;
  }
  const summary = report.summary;
  const measures = summary?.measures;

  const allFindings = report.findings ?? [];
  const securityFindingsAll = allFindings.filter((f) => f.category && SECURITY_FINDING_CATEGORIES.has(f.category));
  const securityFindings = securityFindingsAll.filter((f) =>
    matchesFilter([f.title, f.description, f.rule_id, f.category, f.severity, f.location?.path], securityFilter),
  );
  const otherFindingsAll = allFindings.filter(
    (f) => !(f.category && SECURITY_FINDING_CATEGORIES.has(f.category)) && f.rule_id !== COMPLEXITY_RULE_ID,
  );
  const otherFindings = otherFindingsAll.filter((f) =>
    matchesFilter([f.title, f.description, f.rule_id, f.category, f.severity, f.location?.path], findingsFilter),
  );
  const sbomComponentsAll = report.sbom?.components ?? [];
  const sbomComponents = sbomComponentsAll.filter((c) => matchesFilter([c.name, c.version, c.type, c.purl], sbomFilter));

  const securityGradeValue = measures ? securityGrade(measures.security.worst_open_severity) : "E";
  const dupValue = measures?.duplication_percent;
  const dupGradeValue = dupValue != null ? gradeLowerIsBetter(dupValue, DUP_THRESHOLDS) : null;
  const ccnValue = measures?.mean_ccn;
  const ccnGradeValue = ccnValue != null ? gradeLowerIsBetter(ccnValue, CCN_THRESHOLDS) : null;
  // Commit SHA preferred over branch (see HealthReportPayload.repo's own comment) -- undefined
  // only if the scan somehow recorded neither, in which case CodeLink/sourceUrl below render
  // plain text instead of a broken link.
  const gitRef = report.repo?.commit || job.branch || undefined;
  const sourceUrl = (path: string, startLine?: number | null, endLine?: number | null) =>
    gitRef ? githubFileUrl(owner, repo, gitRef, path, startLine, endLine) : null;

  return (
    <div className="mx-auto w-[96vw] space-y-8 py-6">
      <div>
        <h1 className="text-2xl font-semibold">Repository Health Report — {owner}/{repo}</h1>
        <p className="text-sm text-neutral-500">
          Branch {job.branch} · generated {report.generated_at ?? "—"} ·{" "}
          <span className="font-mono">{report.content_hash?.slice(0, 19) ?? "—"}</span>
        </p>
      </div>

      <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <p className="font-medium">Scope of this report</p>
        <p className="mt-1">
          Fast on-demand scan: test execution, coverage measurement, and DAST/dynamic scans are
          intentionally skipped for turnaround time. The health score below is NOT directly
          comparable to a full workflow run&apos;s score — it omits coverage, dependency-freshness, AC
          verification, accessibility and performance subscores entirely.
        </p>
      </div>

      {summary && (
        <div>
          <h2 className="mb-2 text-lg font-semibold text-neutral-900">Full score breakdown</h2>
          <p className="mb-2 text-sm text-neutral-500">
            All 9 dimensions this platform can measure; dashes below mean this fast scan doesn&apos;t
            measure that dimension (its weight is redistributed across the ones that ARE measured,
            so the overall score is never penalized for something it wasn&apos;t asked to check).
          </p>
          <HealthBreakdown summary={summary} label="Health" />
        </div>
      )}

      {measures && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <DimensionCard
            title="Security"
            chip={
              <Chip
                label="Security"
                value={`${securityGradeValue} · ${securityOpenCount(measures.security.by_severity)}`}
                tone={GRADE_TONE[securityGradeValue]}
              />
            }
            explanation={`Grade reflects the single worst OPEN finding's severity, not a count: A = none open, B = info/low, C = medium, D = high, E = critical. ${
              securityOpenCount(measures.security.by_severity)
            } open finding(s) total.${summary?.health_basis?.security ? ` Score basis: ${summary.health_basis.security}.` : ""}`}
          >
            {securityFindingsAll.length > 5 && (
              <FilterInput value={securityFilter} onChange={setSecurityFilter} placeholder="Filter by title, rule, file..." />
            )}
            {securityFindingsAll.length > 0 ? (
              <FindingsTable findings={securityFindings} sourceUrl={sourceUrl} textSize="sm" />
            ) : (
              <p className="text-sm text-neutral-400">No security findings.</p>
            )}
          </DimensionCard>

          <DimensionCard
            title="Complexity"
            chip={
              ccnGradeValue && ccnValue != null ? (
                <Chip label="Complexity" value={`${ccnGradeValue} · ${ccnValue.toFixed(1)}`} tone={GRADE_TONE[ccnGradeValue]} />
              ) : (
                <Chip label="Complexity" value="—" tone="gray" />
              )
            }
            explanation={`Average cyclomatic complexity (CCN) per function — the number of independent paths through its code; lower means simpler and easier to test. Bands: ${describeBands(CCN_THRESHOLDS, "")}.${
              summary?.health_basis?.complexity ? ` ${summary.health_basis.complexity}.` : ""
            }${
              report.metrics?.complexity?.functions_over_threshold != null
                ? ` ${report.metrics.complexity.functions_over_threshold} of ${report.metrics.complexity.functions_total ?? "?"} function(s) exceed the ${report.metrics.complexity.threshold ?? CCN_THRESHOLDS[3]} threshold below.`
                : ""
            }`}
          >
            <ComplexityTable worst={report.metrics?.complexity?.worst ?? []} owner={owner} repo={repo} gitRef={gitRef} />
          </DimensionCard>

          <DimensionCard
            title="Duplication"
            chip={
              dupGradeValue && dupValue != null ? (
                <Chip label="Duplication" value={`${dupGradeValue} · ${dupValue.toFixed(1)}%`} tone={GRADE_TONE[dupGradeValue]} />
              ) : (
                <Chip label="Duplication" value="—" tone="gray" />
              )
            }
            explanation={`Percent of code lines that are near-duplicates of code elsewhere in the repo (jscpd), 0% is ideal. Bands: ${describeBands(DUP_THRESHOLDS, "%")}.${
              summary?.health_basis?.duplication ? ` ${summary.health_basis.duplication}.` : ""
            }`}
          >
            <DuplicationTable clones={report.metrics?.duplication?.clones ?? []} owner={owner} repo={repo} gitRef={gitRef} />
          </DimensionCard>
        </div>
      )}

      {report.metrics?.churn?.hotspots && report.metrics.churn.hotspots.length > 0 && (
        <div>
          <h2 className="mb-2 text-lg font-semibold text-neutral-900">Hotspots</h2>
          <p className="mb-2 text-sm text-neutral-500">
            Files that are BOTH frequently changed and highly complex — the files most likely to
            hide bugs and most expensive to safely modify. Sorted by (lines changed × max CCN),
            highest risk first.
          </p>
          <HotspotsTable hotspots={report.metrics.churn.hotspots} />
        </div>
      )}

      {report.tools && report.tools.length > 0 && (
        <div>
          <h2 className="mb-2 text-lg font-semibold text-neutral-900">Scanner coverage ({report.tools.length} tool(s))</h2>
          <div className="overflow-x-auto rounded-lg border border-neutral-200">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 text-neutral-500">
                  <th className="py-1.5 pr-3 font-medium">Tool</th>
                  <th className="py-1.5 pr-3 font-medium">Status</th>
                  <th className="py-1.5 pr-3 font-medium">Version</th>
                  <th className="py-1.5 pr-3 font-medium">Duration</th>
                  <th className="py-1.5 pr-3 font-medium">Findings</th>
                  <th className="py-1.5 font-medium">Notes</th>
                </tr>
              </thead>
              <tbody>
                {report.tools.map((t) => (
                  <tr key={t.name} className="border-b border-neutral-100 align-top">
                    <td className="py-1.5 pr-3 font-mono">{t.name}</td>
                    <td className={`py-1.5 pr-3 ${TOOL_STATUS_CLASS[t.status] ?? ""}`}>{t.status}</td>
                    <td className="py-1.5 pr-3 font-mono">{t.version ?? "—"}</td>
                    <td className="py-1.5 pr-3">{t.duration_ms != null ? `${(t.duration_ms / 1000).toFixed(1)}s` : "—"}</td>
                    <td className="py-1.5 pr-3">{t.findings ?? 0}</td>
                    <td className="py-1.5 text-neutral-500">{t.notes || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {otherFindingsAll.length > 0 && (
        <div>
          <h2 className="mb-2 text-lg font-semibold text-neutral-900">Other findings ({otherFindingsAll.length})</h2>
          <p className="mb-2 text-sm text-neutral-500">
            Findings outside Security/Duplication/Complexity above (e.g. maintainability/docs
            checks this scan happened to run).
          </p>
          {otherFindingsAll.length > 5 && (
            <FilterInput value={findingsFilter} onChange={setFindingsFilter} placeholder="Filter by title, rule, file..." />
          )}
          <FindingsTable findings={otherFindings} sourceUrl={sourceUrl} />
        </div>
      )}

      {sbomComponentsAll.length > 0 && (
        <div>
          <h2 className="mb-2 text-lg font-semibold text-neutral-900">
            Software bill of materials ({sbomComponentsAll.length} component(s){report.sbom?.specVersion ? `, CycloneDX ${report.sbom.specVersion}` : ""})
          </h2>
          <FilterInput value={sbomFilter} onChange={setSbomFilter} placeholder="Filter by package name, version, type..." />
          <div className="max-h-96 overflow-auto rounded-lg border border-neutral-200">
            <table className="w-full text-left text-sm">
              <thead className="sticky top-0 bg-neutral-50 text-neutral-500">
                <tr>
                  <th className="px-3 py-1.5 font-medium">Name</th>
                  <th className="px-3 py-1.5 font-medium">Version</th>
                  <th className="px-3 py-1.5 font-medium">Type</th>
                </tr>
              </thead>
              <tbody>
                {sbomComponents.map((c, i) => (
                  <tr key={`${c.purl ?? c.name}-${i}`} className="border-t border-neutral-100">
                    <td className="px-3 py-1.5 font-mono">{c.name ?? "—"}</td>
                    <td className="px-3 py-1.5">{c.version ?? "—"}</td>
                    <td className="px-3 py-1.5">{c.type ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {sbomComponents.length === 0 && <p className="p-3 text-sm text-neutral-400">No components match &quot;{sbomFilter}&quot;.</p>}
          </div>
        </div>
      )}

      <details className="rounded-lg border border-neutral-200 p-3 text-sm">
        <summary className="cursor-pointer font-medium text-neutral-700">Raw scanner evidence</summary>
        <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap font-mono text-xs text-neutral-600">
          {JSON.stringify(report, null, 2)}
        </pre>
      </details>

      <div className="flex gap-2 border-t border-neutral-200 pt-4">
        <button
          type="button"
          className="rounded-lg border border-neutral-300 px-4 py-2 text-sm font-medium text-neutral-700 hover:bg-neutral-50"
          onClick={() =>
            download(
              `health-report-${owner}-${repo}-${jobId}.html`,
              buildStandaloneHtml(job, report, {
                securityFindings: securityFindingsAll,
                otherFindings: otherFindingsAll,
                duplicationClones: report.metrics?.duplication?.clones ?? [],
                complexityWorst: report.metrics?.complexity?.worst ?? [],
                hotspots: report.metrics?.churn?.hotspots ?? [],
                sbomComponents: sbomComponentsAll,
                gitRef,
              }),
              "text/html",
            )
          }
        >
          Download report
        </button>
        <button
          type="button"
          className="rounded-lg border border-neutral-300 px-4 py-2 text-sm font-medium text-neutral-700 hover:bg-neutral-50"
          onClick={() =>
            download(
              `health-report-${owner}-${repo}-${jobId}.sarif.json`,
              JSON.stringify(findingsToSarif(report.findings ?? []), null, 2),
              "application/json",
            )
          }
        >
          Download SARIF
        </button>
      </div>
    </div>
  );
}
