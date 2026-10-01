import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated, sandboxCredentials } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/** Same auth shape as the sibling tabs route: 401 unauthenticated, 404 for both unknown and
 * inaccessible sessions. */
async function authorize(sessionId: string): Promise<NextResponse | null> {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  if (!(await getAuthorizedSession(sessionId))) {
    return NextResponse.json({ error: "not found" }, { status: 404, headers: NO_STORE });
  }
  return null;
}

/** The open human review as a view model (agent/src/review_view.py), proxying the agent's
 * `GET /sessions/{session_id}/review`. */
export async function GET(_request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  const { sessionId } = await params;
  const denied = await authorize(sessionId);
  if (denied) return denied;
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/review`);
  if (!response.ok) {
    return NextResponse.json({ detail: "review unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}

/** Resolves the open review (`POST /sessions/{session_id}/review`): `{stage, action_id, text?}`
 * forwarded as-is -- the agent validates the action and decides the resume value -- plus the same
 * sandbox credentials the provision route forwards, which the agent uses to reconnect the
 * session's sandbox first when an agent restart dropped it. Its status (202/400/409/503) and
 * `detail` pass straight through. */
export async function POST(request: Request, { params }: { params: Promise<{ sessionId: string }> }) {
  const { sessionId } = await params;
  const denied = await authorize(sessionId);
  if (denied) return denied;
  const credentials = await sandboxCredentials();
  if (!credentials) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401, headers: NO_STORE });
  }
  const { stage, action_id, text } = (await request.json()) as { stage?: string; action_id?: string; text?: string };
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage, action_id, text, ...credentials }),
  });
  return NextResponse.json(await response.json().catch(() => ({})), { status: response.status, headers: NO_STORE });
}
