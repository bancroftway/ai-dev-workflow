import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getOctokit } from "@/lib/github";
import { lookupSessionWithAuthorization } from "@/lib/session-access";

/**
 * Files a GitHub issue about a FAILED session into the org-configured support repo (the TOOL's own
 * support/ops repo, org_settings.support_repo — never the customer repo the run worked on). The
 * issue rides the signed-in user's own GitHub token (same getOctokit as every other GitHub write),
 * so a private support repo the user can't reach fails with GitHub's own 404 — surfaced verbatim.
 *
 * Body deliberately excludes stdout/stderr tails: build output can leak env values; support resumes
 * the thread and reads the rest from the work branch. Dedupe: an open issue whose title carries the
 * session's short id short-circuits to that issue's URL instead of filing a duplicate.
 */
export async function POST(request: Request) {
  const { sessionId } = (await request.json()) as { sessionId?: string };
  if (!sessionId) {
    return NextResponse.json({ detail: "sessionId is required" }, { status: 400 });
  }
  const lookup = await lookupSessionWithAuthorization(sessionId);
  if (lookup.kind !== "authorized") {
    return NextResponse.json({ detail: "Session not found" }, { status: 404 });
  }
  const session = lookup.session;
  if (session.status !== "failed") {
    return NextResponse.json({ detail: "Support issues are for failed sessions only" }, { status: 400 });
  }

  const orgResponse = await agentFetch("org-settings");
  const orgSettings = (await orgResponse.json()) as { support_repo?: string | null };
  const supportRepo = orgResponse.ok ? orgSettings.support_repo : null;
  if (!supportRepo) {
    return NextResponse.json(
      { detail: "No support repo configured — set one in organization settings" },
      { status: 409 },
    );
  }
  const [supportOwner, supportName] = supportRepo.split("/");

  const shortId = session.session_id.slice(0, 8);
  const octokit = await getOctokit();
  try {
    const existing = await octokit.rest.search.issuesAndPullRequests({
      q: `repo:${supportRepo} is:issue is:open in:title ${shortId}`,
    });
    const match = existing.data.items[0];
    if (match) {
      return NextResponse.json({ url: match.html_url, existing: true });
    }

    const reportPath = session.run_id
      ? `/sessions/${session.owner}/${session.repo}/${session.session_id}/${session.run_id}/report`
      : null;
    const body = [
      `Automated context for a failed ai-dev-workflow run. Resume this thread to continue.`,
      ``,
      `- Customer repo: ${session.owner}/${session.repo} (branch ${session.source_branch})`,
      `- Thread: ${session.session_id}`,
      `- Run: ${session.run_id ?? "unknown"}`,
      `- Work branch: ${session.work_branch}`,
      `- Failed at: ${session.failure_stage ?? "unknown"} (${session.failure_type ?? "unknown"})`,
      `- Message: ${session.failure_message || "(none recorded)"}`,
      reportPath ? `- Report page: ${reportPath}` : null,
      `- Exit report (if the run reached exit finalize): \`.ai-dev-workflow/EXIT-REPORT.md\` on the work branch`,
    ]
      .filter((line): line is string => line !== null)
      .join("\n");

    const created = await octokit.rest.issues.create({
      owner: supportOwner,
      repo: supportName,
      title: `AI dev workflow run failed at ${session.failure_stage ?? "unknown"} (${session.failure_type ?? "unknown"}) — thread ${shortId}`,
      body,
    });
    return NextResponse.json({ url: created.data.html_url, existing: false });
  } catch (error) {
    const status = (error as { status?: number }).status;
    if (status === 404) {
      return NextResponse.json(
        { detail: `Support repo ${supportRepo} not accessible with your GitHub account` },
        { status: 404 },
      );
    }
    throw error;
  }
}
