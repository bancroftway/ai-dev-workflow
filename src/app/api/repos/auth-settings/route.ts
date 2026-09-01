import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";

/**
 * Per-repo application-auth posture proxy (agent's /repo-auth-settings). Repo-scoped, not
 * per-user: the generated app's auth requirement is a property of the codebase.
 */
export async function GET(request: Request) {
  const token = await getServerAuthToken();
  const login = token?.login ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo are required" }, { status: 400 });
  }
  // Unlike the per-user vault row, this table is (owner, repo)-keyed -- without this check any
  // signed-in user could read another team's auth posture and anonymous-route list.
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const params = new URLSearchParams({ owner, repo });
  const response = await agentFetch(`repo-auth-settings?${params}`);
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { owner, repo, authMode, anonymousRoutes } = (await request.json()) as {
    owner?: string;
    repo?: string;
    authMode?: string;
    anonymousRoutes?: string[];
  };
  if (!owner || !repo || !authMode) {
    return NextResponse.json({ detail: "owner, repo, and authMode are required" }, { status: 400 });
  }
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const response = await agentFetch("repo-auth-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner,
      repo,
      user_login: token.login,
      auth_mode: authMode,
      anonymous_routes: anonymousRoutes ?? [],
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
