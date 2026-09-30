import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * Per-check fail rates + per-stage attempts-to-pass for one repo (gate dialog hints, insights
 * page), proxying the agent's `GET /repos/{owner}/{repo}/verify-insights?since=&mode=`. Repo
 * access is the caller's own GitHub token, same as every repo-scoped route (session-access.ts).
 */
export async function GET(request: Request, { params }: { params: Promise<{ owner: string; repo: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  const { owner, repo } = await params;
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403, headers: NO_STORE });
  }
  const incoming = new URL(request.url).searchParams;
  const query = new URLSearchParams();
  for (const key of ["since", "mode"]) {
    const value = incoming.get(key);
    if (value) query.set(key, value);
  }
  const qs = query.size ? `?${query}` : "";
  const response = await agentFetch(
    `repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/verify-insights${qs}`,
  );
  // 422 = a bad since/mode filter -- pass the agent's own message through.
  if (response.status === 422) {
    return NextResponse.json(await response.json(), { status: 422, headers: NO_STORE });
  }
  if (!response.ok) {
    return NextResponse.json({ detail: "verify insights unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}
