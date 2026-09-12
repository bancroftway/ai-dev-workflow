import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

/**
 * Passthrough for the agent's read-only checkpoint peek (sessions_api.get_checkpoint_state) --
 * same auth/ownership gate as `[sessionId]/route.ts` right next to this file. Lets the workflow
 * page hydrate `agent.state` (via `agent.setState`) for a session nothing is currently driving,
 * without ever calling `runAgent`.
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

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/checkpoint-state`);
  const body = await response.json().catch(() => ({}));
  return NextResponse.json(body, { status: response.status });
}
