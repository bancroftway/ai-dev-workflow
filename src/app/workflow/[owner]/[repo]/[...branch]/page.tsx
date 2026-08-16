import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";
import { AppShell } from "@/components/AppShell";
import { SandboxSessionBoot } from "@/components/SandboxSessionBoot";
import { WorkspaceHeader } from "@/components/WorkspaceHeader";
import { deriveThreadId } from "@/lib/workflow-thread";
import { WorkflowThreadProvider } from "@/lib/workflow-thread-context";
import { SandboxStatusProvider } from "@/lib/sandbox-status-context";
import { WorkflowProviders } from "../../../providers";

export default async function WorkflowPage({
  params,
}: {
  params: Promise<{ owner: string; repo: string; branch: string[] }>;
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

  // Metrics-bar color thresholds, read server-side at request time (NOT NEXT_PUBLIC_*, which
  // would be inlined at build time and unchangeable in a deployed image). Edit .env locally or
  // the container env in deployment, restart, done. Defaults here mirror the .env seeds.
  const env = (name: string, fallback: number) => {
    const v = Number(process.env[name]);
    return Number.isFinite(v) ? v : fallback;
  };
  const metricThresholds = {
    healthGreen: env("METRIC_HEALTH_GREEN", 80),
    healthAmber: env("METRIC_HEALTH_AMBER", 60),
    coverageGreen: env("METRIC_COVERAGE_GREEN", 80),
    coverageAmber: env("METRIC_COVERAGE_AMBER", 60),
    dupGreen: env("METRIC_DUP_GREEN", 3),
    dupAmber: env("METRIC_DUP_AMBER", 5),
    secGreen: env("METRIC_SEC_GREEN", 0),
    secAmber: env("METRIC_SEC_AMBER", 2),
  };

  return (
    <WorkflowThreadProvider threadId={threadId}>
      <WorkflowProviders>
        <SandboxStatusProvider>
          <div className="flex min-h-full flex-1 flex-col">
            <WorkspaceHeader />
            <SandboxSessionBoot owner={owner} repo={repo} branch={branch} />
            <AppShell metricThresholds={metricThresholds} />
          </div>
        </SandboxStatusProvider>
      </WorkflowProviders>
    </WorkflowThreadProvider>
  );
}
