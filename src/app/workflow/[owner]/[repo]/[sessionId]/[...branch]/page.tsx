import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";
import { AppShell } from "@/components/AppShell";
import { SandboxSessionBoot } from "@/components/SandboxSessionBoot";
import { WorkflowThreadProvider } from "@/lib/workflow-thread-context";
import { SandboxStatusProvider } from "@/lib/sandbox-status-context";
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
  };

  return (
    <WorkflowThreadProvider threadId={sessionId}>
      <WorkflowProviders>
        <SandboxStatusProvider>
          {/* The page shell (header, frozen/scroll split) lives once in root layout now -- this
              is just this route's own content, filling whatever height that shell hands it. */}
          <div className="flex h-full w-full flex-col">
            <div className="shrink-0">
              <SandboxSessionBoot
                sessionId={sessionId}
                owner={owner}
                repo={repo}
                branch={branch}
                resume={resume}
                projectId={projectId}
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
              />
            </div>
          </div>
        </SandboxStatusProvider>
      </WorkflowProviders>
    </WorkflowThreadProvider>
  );
}
