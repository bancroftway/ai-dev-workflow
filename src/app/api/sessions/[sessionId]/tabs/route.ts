import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * The stage tab strip (each tab's enabled flag, status tone and gate icon), built server-side
 * (agent/src/gate_view.py), proxying the agent's `GET /sessions/{session_id}/tabs`. Same auth
 * shape as the sibling events/summary route: 401 unauthenticated, 404 for both unknown and
 * inaccessible sessions.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  const { sessionId } = await params;
  if (!(await getAuthorizedSession(sessionId))) {
    return NextResponse.json({ error: "not found" }, { status: 404, headers: NO_STORE });
  }
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/tabs`);
  if (!response.ok) {
    return NextResponse.json({ detail: "tabs unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}
