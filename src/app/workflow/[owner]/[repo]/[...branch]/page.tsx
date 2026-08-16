import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";
import { AppShell } from "@/components/AppShell";
import { SandboxSessionBoot } from "@/components/SandboxSessionBoot";
import { WorkspaceHeader } from "@/components/WorkspaceHeader";
import { deriveThreadId } from "@/lib/workflow-thread";
import { WorkflowThreadProvider } from "@/lib/workflow-thread-context";
import { SandboxStatusProvider } from "@/lib/sandbox-status-context";
import { parseThresholds } from "@/lib/metric-grades";
import { WorkflowProviders } from "../../../providers";

export default async function WorkflowPage({
  params,
  searchParams,
}: {
  params: Promise<{ owner: string; repo: string; branch: string[] }>;
  searchParams: Promise<{ resume?: string }>;
}) {
  const session = await auth();
  const githubId = session?.githubId ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!githubId) {
    redirect("/");
  }

  const { owner, repo, branch: branchSegments } = await params;
  // Branch names may contain "/" (e.g. "feature/foo") -- the catch-all segment above captures
  // every segment after [repo], which this rejoins into the real branch name.
  const branch = branchSegments.join("/");
  const threadId = deriveThreadId(owner, repo, githubId);

  // Set by SessionHistory's Resume button (/select) as ?resume=1 -- forwarded into the
  // provision POST body (SandboxSessionBoot) and used to unconditionally fire the first run once
  // the sandbox is ready (AppShell), even on a thread that already has state.
  const resume = (await searchParams).resume === "1";

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
    <WorkflowThreadProvider threadId={threadId}>
      <WorkflowProviders>
        <SandboxStatusProvider>
          <div className="flex min-h-full flex-1 flex-col">
            <WorkspaceHeader />
            <SandboxSessionBoot owner={owner} repo={repo} branch={branch} resume={resume} />
            <AppShell metricThresholds={metricThresholds} resume={resume} />
          </div>
        </SandboxStatusProvider>
      </WorkflowProviders>
    </WorkflowThreadProvider>
  );
}
