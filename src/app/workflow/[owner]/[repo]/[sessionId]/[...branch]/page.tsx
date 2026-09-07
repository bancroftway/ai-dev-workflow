import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";
import { AppShell } from "@/components/AppShell";
import type { FilesChangedSummary, ReportExtras } from "@/components/ReportView";
import { SandboxSessionBoot } from "@/components/SandboxSessionBoot";
import { WorkflowThreadProvider } from "@/lib/workflow-thread-context";
import { SandboxStatusProvider } from "@/lib/sandbox-status-context";
import { RunActivityProvider } from "@/lib/run-activity-context";
import { getOctokit, readRepoFile } from "@/lib/github";
import { parseThresholds } from "@/lib/metric-grades";
import { lookupSessionWithAuthorization } from "@/lib/session-access";
import { WorkflowProviders } from "../../../../providers";

export default async function WorkflowPage({
  params,
  searchParams,
}: {
  params: Promise<{ owner: string; repo: string; sessionId: string; branch: string[] }>;
  searchParams: Promise<{ resume?: string; projectId?: string }>;
}) {
  const session = await auth();
  const githubId = session?.githubId ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!githubId) {
    redirect("/");
  }

  const { owner, repo, sessionId, branch: branchSegments } = await params;
  // Branch names may contain "/" (e.g. "feature/foo") -- the catch-all segment above captures
  // every segment after [sessionId], which this rejoins into the real branch name.
  const branch = branchSegments.join("/");

  // Ownership check: sessionId is a random UUID now (no longer deterministically derived from
  // (owner, repo, githubId)), so landing on someone else's session id here is no longer
  // structurally impossible the way it used to be. "not_found" is the only outcome that proceeds
  // (a brand-new session SandboxSessionBoot is about to provision, client-side, after this page
  // renders) -- "denied" must hard-redirect rather than fall through the same way, since this
  // page renders AppShell against sessionId as a LangGraph thread_id next, and that checkpointer
  // has no owner/repo check of its own.
  const lookup = await lookupSessionWithAuthorization(sessionId);
  if (lookup.kind === "denied") {
    redirect("/select");
  }
  const sessionRow = lookup.kind === "authorized" ? lookup.session : null;
  if (sessionRow && (sessionRow.owner !== owner || sessionRow.repo !== repo)) {
    redirect("/select");
  }

  const resolvedSearchParams = await searchParams;
  // Set by SessionHistory's Resume button (/select) as ?resume=1 -- forwarded into the
  // provision POST body (SandboxSessionBoot) and used to unconditionally fire the first run once
  // the sandbox is ready (AppShell), even on a thread that already has state.
  const resume = resolvedSearchParams.resume === "1";
  // Set by /select's RepoBranchSection ("start new session", Task 5) after it resolves a real
  // project_id via POST /api/projects/connect -- only present for that brand-new-session case,
  // forwarded as-is into SandboxSessionBoot's own provision call. Undefined for every other entry
  // into this page (resume, a plain reload); provision_session falls back to the session's own
  // already-stored project_id then, so this route needs no other-case handling of its own.
  const projectId = resolvedSearchParams.projectId;

  // Metrics-bar grade band thresholds, read server-side at request time (NOT NEXT_PUBLIC_*, which
  // would be inlined at build time and unchangeable in a deployed image). Edit .env locally or
  // the container env in deployment, restart, done. Defaults here mirror the .env seeds.
  // parseThresholds validates each CSV var and falls back (with a console.warn) on bad input --
  // this is the only place it's called, so "warn once server-side" holds.
  const metricThresholds = {
    ccn: parseThresholds(process.env.METRIC_CCN_GRADES, [5, 10, 15, 20], "METRIC_CCN_GRADES", true),
    coverage: parseThresholds(process.env.METRIC_COVERAGE_GRADES, [80, 70, 50, 30], "METRIC_COVERAGE_GRADES", false),
    dup: parseThresholds(process.env.METRIC_DUP_GRADES, [3, 5, 10, 20], "METRIC_DUP_GRADES", true),
    lhPerf: parseThresholds(process.env.METRIC_LH_PERF_GRADES, [90, 75, 60, 40], "METRIC_LH_PERF_GRADES", false),
    a11y: parseThresholds(process.env.METRIC_A11Y_GRADES, [95, 90, 80, 60], "METRIC_A11Y_GRADES", false),
  };

  // The pieces of a completed session's exit report that live only in the committed report.json,
  // never in live LangGraph state (every other Report-tab field reads live state). Same
  // source/shape the now-deleted standalone /report route used to read.
  let filesChanged: FilesChangedSummary | null = null;
  let reportExtras: ReportExtras | null = null;
  if (sessionRow?.status === "completed" && sessionRow.run_id) {
    const octokit = await getOctokit();
    const reportRaw = await readRepoFile(
      octokit, owner, repo, `.ai-dev-workflow/history/${sessionRow.run_id}-report.json`, sessionRow.work_branch,
    );
    if (reportRaw) {
      try {
        const parsed = JSON.parse(reportRaw) as {
          files_changed?: string;
          commits?: string;
          stage_summary?: ReportExtras["stageSummary"];
          us_ac?: ReportExtras["usAc"];
          metrics?: {
            repo_scan?: { findings?: ReportExtras["findings"] };
            ac_execution?: { per_ac?: ReportExtras["acExecutionPerAc"] };
          };
        };
        filesChanged = { stat: parsed.files_changed, commits: parsed.commits };
        reportExtras = {
          stageSummary: parsed.stage_summary,
          usAc: parsed.us_ac,
          findings: parsed.metrics?.repo_scan?.findings,
          acExecutionPerAc: parsed.metrics?.ac_execution?.per_ac,
        };
      } catch {
        filesChanged = null;
        reportExtras = null;
      }
    }
  }

  return (
    <WorkflowThreadProvider threadId={sessionId}>
      <WorkflowProviders>
        <SandboxStatusProvider>
          <RunActivityProvider>
            {/* The page shell (header, frozen/scroll split) lives once in root layout now -- this
                is just this route's own content, filling whatever height that shell hands it. */}
            <div className="flex h-full w-full flex-col">
              <div className="shrink-0">
                {/* Always mounted (it's the one writer of sandboxStatus -- see its own `skip` prop
                    doc for why skipping the mount too would strand the header's pill on
                    "Connecting…" forever). `skip` is true for a terminal (completed/failed/
                    rejected) session opened WITHOUT ?resume=1: this used to POST
                    /api/sessions/provision unconditionally for EVERY session, including one whose
                    container/branch may be long gone (the reason completed sessions used to route
                    to the now-deleted standalone /report page instead of here in the first
                    place). */}
                <SandboxSessionBoot
                  sessionId={sessionId}
                  owner={owner}
                  repo={repo}
                  branch={branch}
                  resume={resume}
                  projectId={projectId}
                  skip={Boolean(sessionRow) && sessionRow?.status !== "in_progress" && !resume}
                />
              </div>
              {/* min-h-0 is required here, not decorative: without it a flex child's default
                  min-height:auto lets it grow past this row's share of the column instead of
                  bounding to it, which is what AppShell's own internal scroll region depends on. */}
              <div className="min-h-0 flex-1">
                <AppShell
                  owner={owner}
                  repo={repo}
                  // Not yet provisioned (sessionRow is null): no artifacts exist to read yet either,
                  // so an empty string is never actually dereferenced against GitHub.
                  workBranch={sessionRow?.work_branch ?? ""}
                  metricThresholds={metricThresholds}
                  resume={resume}
                  filesChanged={filesChanged}
                  reportExtras={reportExtras}
                />
              </div>
            </div>
          </RunActivityProvider>
        </SandboxStatusProvider>
      </WorkflowProviders>
    </WorkflowThreadProvider>
  );
}
