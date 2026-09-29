import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_GITHUB_ID, E2E_MODE, githubAccessToken } from "@/lib/e2e";

/**
 * Server-to-server proxy into the agent's on-demand health-report endpoint. Same shape as
 * sessions/provision/route.ts: the browser never holds or sends the GitHub access token.
 *
 * No `hasRepoAccess` check here, matching sessions/provision/route.ts's own precedent (see that
 * route's comment) -- the forwarded token is what the sandbox clones the target repo with, so a
 * caller without real read access to (owner, repo) simply fails at clone time inside the agent,
 * the same enforcement every session provision already relies on. The report's CONTENTS are the
 * actual access boundary (real scanner findings, file paths) -- that check lives on the GET route
 * below, once a job exists to check access against.
 */
export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const accessToken = githubAccessToken(token);
  const userLogin = token?.login ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { owner, repo, branch } = (await request.json()) as {
    owner?: string;
    repo?: string;
    branch?: string;
  };
  if (!owner || !repo || !branch) {
    return NextResponse.json({ error: "owner, repo, and branch are required" }, { status: 400 });
  }

  const response = await agentFetch("health-reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner,
      repo,
      branch,
      github_token: accessToken,
      user_login: userLogin ?? "",
      entra_assertion: token?.entraAccessToken ?? null,
    }),
  });

  const body = await response.json();
  return NextResponse.json(body, { status: response.status });
}
