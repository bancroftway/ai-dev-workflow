import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";

/**
 * Saves which vault secrets are exposed to the sandbox and under which env names. The agent
 * re-validates env names server-side (they end up in a shell-sourced file -- a security
 * boundary, not a style rule); this proxy only shapes the request.
 */
export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { owner, repo, selection } = (await request.json()) as {
    owner?: string;
    repo?: string;
    selection?: Array<{ name?: string; env_name?: string | null }>;
  };
  if (!owner || !repo || !Array.isArray(selection)) {
    return NextResponse.json({ detail: "owner, repo, and selection are required" }, { status: 400 });
  }
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const response = await agentFetch("vault-config/selection", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, user_login: token.login, selection }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
