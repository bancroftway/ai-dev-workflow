import { redirect } from "next/navigation";
import { auth } from "@/auth";
import { AppShell } from "@/components/AppShell";
import { SandboxSessionBoot } from "@/components/SandboxSessionBoot";
import { WorkspaceHeader } from "@/components/WorkspaceHeader";
import { deriveThreadId } from "@/lib/workflow-thread";
import { WorkflowThreadProvider } from "@/lib/workflow-thread-context";
import { WorkflowProviders } from "../../../providers";

export default async function WorkflowPage({
  params,
}: {
  params: Promise<{ owner: string; repo: string; branch: string[] }>;
}) {
  const session = await auth();
  if (!session?.githubId) {
    redirect("/");
  }

  const { owner, repo, branch: branchSegments } = await params;
  // Branch names may contain "/" (e.g. "feature/foo") -- the catch-all segment above captures
  // every segment after [repo], which this rejoins into the real branch name.
  const branch = branchSegments.join("/");
  const threadId = deriveThreadId(owner, repo, branch, session.githubId);

  return (
    <WorkflowThreadProvider threadId={threadId}>
      <WorkflowProviders>
        <div className="flex min-h-full flex-1 flex-col">
          <WorkspaceHeader />
          <SandboxSessionBoot owner={owner} repo={repo} branch={branch} />
          <AppShell />
        </div>
      </WorkflowProviders>
    </WorkflowThreadProvider>
  );
}
