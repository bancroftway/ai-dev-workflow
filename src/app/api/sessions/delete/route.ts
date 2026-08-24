import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { githubAccessToken } from "@/lib/e2e";
import { lookupSessionWithAuthorization } from "@/lib/session-access";

/**
 * Full purge (SessionHistory's "Delete" button): stops the container if one is running, deletes
 * the session's own GitHub work branch, and removes its history row entirely -- distinct from
 * "Stop container" (WorkspaceHeader), which only tears down the sandbox and keeps everything
 * else. Forwards the CALLER's live GitHub token so the agent can delete the branch even for a
 * long-idle session whose in-memory push-token cache is long gone.
 */
export async function POST(request: Request) {
  const { sessionId } = (await request.json()) as { sessionId?: string };
  if (!sessionId) {
    return NextResponse.json({ detail: "sessionId is required" }, { status: 400 });
  }

  const lookup = await lookupSessionWithAuthorization(sessionId);
  if (lookup.kind !== "authorized") {
    return NextResponse.json({ detail: "session not found" }, { status: 404 });
  }

  const token = await getServerAuthToken();
  const githubToken = githubAccessToken(token);

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ github_token: githubToken ?? "" }),
  });
  const body = await response.json().catch(() => ({}));
  return NextResponse.json(body, { status: response.status });
}
