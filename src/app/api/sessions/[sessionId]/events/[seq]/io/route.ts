import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

/**
 * Overview-tab redraft history (Workstream 3): on-demand fetch of one event's full input/output
 * text, proxying to the agent's `GET /sessions/{id}/events/{seq}/io` (sessions_api.py). Same
 * auth/ownership gate as `[sessionId]/events/stream/route.ts` right next to this file -- plain
 * JSON passthrough, not a stream, since this is a single request fired on hover, not a live feed.
 */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string; seq: string }> }) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId, seq } = await params;
  const sessionRow = await getAuthorizedSession(sessionId);
  if (!sessionRow) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(seq)}/io`);
  const body = await response.text();
  return new NextResponse(body, {
    status: response.status,
    headers: { "Content-Type": response.headers.get("content-type") ?? "application/json" },
  });
}
