import type { Octokit } from "octokit";
import { redirect } from "next/navigation";
import { ReportView, type FilesChangedSummary } from "@/components/ReportView";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";
import { getOctokit } from "@/lib/github";
import { parseThresholds } from "@/lib/metric-grades";
import { rawProxyUrl } from "@/lib/raw-proxy";
import type { DeltaSummary, MergeReadinessReport, MetricsReportState } from "@/lib/workflow-types";

// Must match entrypoint.sh's WORK_BRANCH / agent/src/git_ops.py's _WORK_BRANCH constant -- every
// history/ artifact lives on this single repo-shared branch, never the user's own branch.
const WORK_BRANCH = "ai-dev-workflow";

// run_id is intake_node's uuid4().hex[:8] (agent/src/graph.py) -- 8 lowercase hex chars. The
// range is a little generous (6-12) to tolerate a length tweak on the agent side without this
// route needing a matching edit.
const RUN_ID_RE = /^[a-z0-9]{6,12}$/;

async function readRepoFile(octokit: Octokit, owner: string, repo: string, path: string): Promise<string | null> {
  try {
    const res = await octokit.rest.repos.getContent({ owner, repo, path, ref: WORK_BRANCH });
    if (Array.isArray(res.data) || res.data.type !== "file" || !res.data.content) return null;
    return Buffer.from(res.data.content, "base64").toString("utf-8");
  } catch (error) {
    // Covers both "this file was never written" and "the ai-dev-workflow branch doesn't exist yet"
    // -- neither is an error worth a 500, both just mean there's nothing to show here.
    if ((error as { status?: number }).status === 404) return null;
    throw error;
  }
}

export default async function RunReportPage({
  params,
}: {
  params: Promise<{ owner: string; repo: string; runId: string }>;
}) {
  const session = await auth();
  const githubId = session?.githubId ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!githubId) {
    redirect("/");
  }

  const { owner, repo, runId } = await params;
  if (!RUN_ID_RE.test(runId)) {
    return <NotFoundPanel owner={owner} repo={repo} />;
  }

  const octokit = await getOctokit();
  const historyPath = (suffix: string) => `.ai-dev-workflow/history/${runId}-${suffix}`;

  const reportRaw = await readRepoFile(octokit, owner, repo, historyPath("report.json"));

  let report: MergeReadinessReport | null = null;
  let metrics: MetricsReportState["metrics"] | undefined;
  let deltaSummary: DeltaSummary | null = null;
  let filesChanged: FilesChangedSummary | null = null;
  let screenshots: string[] = [];

  if (reportRaw) {
    // Primary path: the durable per-run artifact agent/src/exit_nodes.py's exit_finalize_node
    // writes, carrying everything ReportView needs in one shot.
    let parsed: Record<string, unknown> = {};
    try {
      parsed = JSON.parse(reportRaw) as Record<string, unknown>;
    } catch {
      parsed = {};
    }
    report = (parsed.merge_readiness as MergeReadinessReport | null | undefined) ?? null;
    metrics = parsed.metrics as MetricsReportState["metrics"] | undefined;
    deltaSummary = (parsed.delta_summary as DeltaSummary | null | undefined) ?? null;
    filesChanged = { stat: parsed.files_changed as string | undefined, commits: parsed.commits as string | undefined };
    screenshots = Array.isArray(parsed.screenshots) ? (parsed.screenshots as string[]) : [];
  } else {
    // Fallback: report.json is absent -- either this run predates it, or the run never reached
    // exit finalize. Reconstruct per-file, noting gaps rather than fabricating them.
    const metricsRaw = await readRepoFile(octokit, owner, repo, historyPath("metrics.json"));
    const exitMdRaw = await readRepoFile(octokit, owner, repo, historyPath("exit.md"));

    if (!metricsRaw && !exitMdRaw) {
      return <NotFoundPanel owner={owner} repo={repo} />;
    }

    if (metricsRaw) {
      try {
        metrics = JSON.parse(metricsRaw) as MetricsReportState["metrics"];
      } catch {
        metrics = undefined;
      }
    }
    // The DeltaSummary rollup (fixed/introduced/severity_changed/metrics) is computed by
    // agent/src/repo_scan.py's delta_summary(), a Python-only transform over the raw diff scan --
    // not recomputable here, so a pre-report.json metrics.json can't recover it. Left null;
    // ReportView already renders "no baseline recorded" for that case.
    deltaSummary = null;
    report = exitMdRaw
      ? {
          merge_ready: false,
          blocking_reasons: [],
          pr_title: "Exit summary (structured report not recorded for this run)",
          pr_description_markdown: exitMdRaw,
          risk_notes: [],
        }
      : null;
    filesChanged = null;
  }

  // Same env-driven thresholds as the live workflow page (src/app/workflow/.../page.tsx) --
  // threading them here rather than falling back to metric-grades.ts's own defaults keeps grading
  // identical whether a metric is read live or from a past session's committed artifact.
  const metricThresholds = {
    ccn: parseThresholds(process.env.METRIC_CCN_GRADES, [5, 10, 15, 20], "METRIC_CCN_GRADES", true),
    coverage: parseThresholds(process.env.METRIC_COVERAGE_GRADES, [80, 70, 50, 30], "METRIC_COVERAGE_GRADES", false),
    dup: parseThresholds(process.env.METRIC_DUP_GRADES, [3, 5, 10, 20], "METRIC_DUP_GRADES", true),
  };

  return (
    <ReportView
      report={report}
      metrics={metrics}
      deltaSummary={deltaSummary}
      filesChanged={filesChanged}
      screenshotUrls={screenshots.map((path) => rawProxyUrl(owner, repo, path))}
      thresholds={metricThresholds}
    />
  );
}

function NotFoundPanel({ owner, repo }: { owner: string; repo: string }) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-2 p-10 text-center">
      <h1 className="text-lg font-semibold">No report found for this run</h1>
      <p className="text-sm text-neutral-500">
        Nothing was recorded for this run on {owner}/{repo}, or the ai-dev-workflow branch doesn&apos;t exist yet.
      </p>
    </div>
  );
}
