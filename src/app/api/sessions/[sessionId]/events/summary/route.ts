import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

/**
 * Overview-tab fix (2026-09-22): server-computed per-stage/per-rebuild-placement duration+cost
 * summary, proxying the agent's `GET /sessions/{session_id}/events/summary`
 * (sessions_api.py's get_session_summary). Same auth/ownership shape as the sibling
 * `[sessionId]/events/route.ts` right next to this file -- 401 with no signed-in session, 404
 * for both "no such session" and "exists but you lack GitHub access to its repo"
 * (getAuthorizedSession collapses those on purpose -- see session-access.ts).
 */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId } = await params;
  const sessionRow = await getAuthorizedSession(sessionId);
  if (!sessionRow) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/events/summary`);
  if (!response.ok) {
    return NextResponse.json({ error: "failed to fetch summary" }, { status: response.status });
  }
  return NextResponse.json(await response.json());
}
