import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * One verification gate's ready-to-render screen (agent/src/gate_view.py), proxying the agent's
 * `GET /sessions/{session_id}/gates/{tab_id}?attempt=`. Same auth shape as the sibling tabs
 * route; the agent's 404 (a tab that gates nothing) passes through as 404.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ sessionId: string; tabId: string }> },
) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  const { sessionId, tabId } = await params;
  if (!(await getAuthorizedSession(sessionId))) {
    return NextResponse.json({ error: "not found" }, { status: 404, headers: NO_STORE });
  }
  const attempt = new URL(request.url).searchParams.get("attempt");
  const query = attempt ? `?attempt=${encodeURIComponent(attempt)}` : "";
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/gates/${encodeURIComponent(tabId)}${query}`);
  if (response.status === 404) {
    return NextResponse.json({ error: "not found" }, { status: 404, headers: NO_STORE });
  }
  if (!response.ok) {
    return NextResponse.json({ detail: "gate unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}
