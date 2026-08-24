import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { githubAccessToken } from "@/lib/e2e";

/**
 * Connect-a-Repository proxy (Task 5's own form calls this; built now alongside its sibling
 * ../route.ts since both are thin proxies onto the same Part 3 projects_router). Same
 * server-derived created_by pattern as ../route.ts's POST and ../../settings/organization/route.ts
 * before it -- never trusted from the client body.
 *
 * Also the /select "start new session"/"resume" actions' own connect-or-find-existing-project
 * step (Task 5) -- not just the standalone Connect-Repository button.
 */
export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const createdBy = auditIdentity(token);
  if (!createdBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  // Needed server-side so the agent can look up the repo's real default branch on GitHub.
  const githubToken = githubAccessToken(token);
  if (!githubToken) {
    return NextResponse.json({ detail: "GitHub is not connected" }, { status: 401 });
  }

  const { owner, repo } = (await request.json()) as { owner?: string; repo?: string };
  if (!owner || !repo) {
    return NextResponse.json({ detail: "owner and repo are required" }, { status: 400 });
  }

  const response = await agentFetch("projects/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, created_by: createdBy, github_token: githubToken }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
