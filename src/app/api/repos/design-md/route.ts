import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { requireRepoAccess } from "@/lib/session-access";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";

/**
 * Per-repo DESIGN.md override proxy (agent's /repo-design-settings). Repo-scoped, not per-user, so
 * hasRepoAccess gates GET as well as PUT -- same reasoning as ../test-config/route.ts.
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
  const accessError = await requireRepoAccess(owner, repo);
  if (accessError) return accessError;
  const params = new URLSearchParams({ owner, repo });
  const response = await agentFetch(`repo-design-settings?${params}`);
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { owner, repo, design_md } = (await request.json()) as {
    owner?: string;
    repo?: string;
    design_md?: string | null;
  };
  if (!owner || !repo) {
    return NextResponse.json({ detail: "owner and repo are required" }, { status: 400 });
  }
  const accessError = await requireRepoAccess(owner, repo);
  if (accessError) return accessError;
  const response = await agentFetch("repo-design-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner,
      repo,
      design_md: typeof design_md === "string" && design_md.trim() ? design_md : null,
      updated_by: token.login,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
