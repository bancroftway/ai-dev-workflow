import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

const NO_STORE = { "Cache-Control": "no-store" };

export type ActiveSessionEntry = { owner: string; repo: string; session_id: string };

/**
 * Which repos have a live sandbox container right now -- one call for /select's whole repo list
 * (the per-repo container cap's badge + disabled "Start new session"). Proxies the agent's
 * `GET /sessions/active` (in-process registry joined to dbo.sessions).
 *
 * Deliberately NO per-row hasRepoAccess filtering (contrast list/route.ts): that check is one
 * GitHub API round-trip per repo, and this route is polled every 15s against the full list. Any
 * signed-in user can therefore see WHICH owner/repo names currently hold a container -- accepted
 * for an internal single-org tool; the session data itself stays behind list/route.ts's check.
 */
export async function GET() {
  const session = await auth();
  if (!session && !E2E_MODE) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const response = await agentFetch("sessions/active");
  if (!response.ok) {
    return NextResponse.json({ active: [] }, { headers: NO_STORE });
  }
  const body = (await response.json()) as { active: ActiveSessionEntry[] };
  return NextResponse.json(body, { headers: NO_STORE });
}
