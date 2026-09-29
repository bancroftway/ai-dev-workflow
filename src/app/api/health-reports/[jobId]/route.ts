import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess, isAuthenticated } from "@/lib/session-access";

/**
 * Single health-report-job lookup, polled by the report page. A finished report bundles real
 * scanner output (gitleaks hits, semgrep findings, file paths) behind a bare job_id -- a random
 * UUID with no owner/repo check of its own on the agent side (same posture as a session id, see
 * session-access.ts's own module comment). Mirrors sessions/[sessionId]/route.ts exactly: fetch
 * the job first (it carries owner/repo), then gate on the caller's OWN GitHub access to that
 * repo, and collapse "no such job" and "exists but you can't see it" into the same 404 -- never
 * confirm a job's existence to a caller who can't see its repo.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ jobId: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { jobId } = await params;
  const response = await agentFetch(`health-reports/${encodeURIComponent(jobId)}`);
  if (response.status === 404) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  if (!response.ok) {
    return NextResponse.json({ error: "not found" }, { status: 404 }); // fail closed, same as getAuthorizedSession
  }
  const job = (await response.json()) as { owner: string; repo: string; [key: string]: unknown };
  if (!(await hasRepoAccess(job.owner, job.repo))) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }
  return NextResponse.json(job);
}
