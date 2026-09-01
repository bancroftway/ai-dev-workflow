import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";

/**
 * Per-repo test users proxy (agent's /repo-test-users). Repo-scoped, so hasRepoAccess gates GET as
 * well as PUT. No passwords ever cross this boundary -- only name/email/roles.
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
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const params = new URLSearchParams({ owner, repo });
  const response = await agentFetch(`repo-test-users?${params}`);
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { owner, repo, users } = (await request.json()) as {
    owner?: string;
    repo?: string;
    users?: unknown[];
  };
  if (!owner || !repo) {
    return NextResponse.json({ detail: "owner and repo are required" }, { status: 400 });
  }
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const response = await agentFetch("repo-test-users", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, user_login: token.login, users: users ?? [] }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
