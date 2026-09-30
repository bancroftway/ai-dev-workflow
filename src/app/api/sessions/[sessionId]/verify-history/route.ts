import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * Every recorded verify attempt for this session (the gate dialog's Attempt selector), proxying
 * the agent's `GET /sessions/{session_id}/verify-history?stage=`. Same auth shape as the sibling
 * events/summary route: 401 unauthenticated, 404 for both unknown and inaccessible sessions.
 */
export async function GET(request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  const { sessionId } = await params;
  if (!(await getAuthorizedSession(sessionId))) {
    return NextResponse.json({ error: "not found" }, { status: 404, headers: NO_STORE });
  }
  const stage = new URL(request.url).searchParams.get("stage");
  const query = stage ? `?stage=${encodeURIComponent(stage)}` : "";
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/verify-history${query}`);
  if (!response.ok) {
    return NextResponse.json({ detail: "verify history unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}
