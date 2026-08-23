import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";
import { getAuthorizedSession } from "@/lib/session-access";

/**
 * This session's durable event history (Part 2 Task 8's EventLogView) -- the fetch-based fallback
 * for a finished run or a fresh page load/reconnect, since the live AG-UI custom-event channel
 * (Task 2) only delivers events while this browser tab is watching an actively-running turn.
 * Proxies to the agent's `GET /sessions/{session_id}/events` (sessions_api.py), which itself keys
 * off session_id rather than a stored run_id -- see that route's own docstring for why (a session
 * can accumulate more than one run_id across resumes).
 *
 * Same auth/ownership shape as `[sessionId]/route.ts` right next to this file: 401 with no signed-in
 * session (E2E bypass aside), 404 for both "no such session" and "exists but you lack GitHub access
 * to its repo" (getAuthorizedSession collapses those on purpose -- see session-access.ts).
 */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  const session = await auth();
  if (!session && !E2E_MODE) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId } = await params;
  const sessionRow = await getAuthorizedSession(sessionId);
  if (!sessionRow) {
    return NextResponse.json({ error: "not found" }, { status: 404 });
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/events`);
  if (!response.ok) {
    return NextResponse.json({ error: "failed to fetch events" }, { status: response.status });
  }
  return NextResponse.json(await response.json());
}
