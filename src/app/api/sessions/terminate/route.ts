import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { lookupSessionWithAuthorization } from "@/lib/session-access";

/**
 * Manual "stop container" proxy (WorkspaceHeader's connection indicator). Forwards to the agent's
 * existing DELETE /sessions/{id} (terminate_session) -- same explicit-close path SessionHistory's
 * teardown already uses elsewhere, which discards the sandbox AND its persistent workspace
 * volume (idle reaps deliberately keep the workspace; an explicit stop does not). Authorization
 * mirrors the other session-scoped routes: same repo-access check as resuming a session.
 */
export async function POST(request: Request) {
  const { sessionId } = (await request.json()) as { sessionId?: string };
  if (!sessionId) {
    return NextResponse.json({ detail: "sessionId is required" }, { status: 400 });
  }

  const lookup = await lookupSessionWithAuthorization(sessionId);
  if (lookup.kind !== "authorized") {
    return NextResponse.json({ detail: "session not found" }, { status: 404 });
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  const body = await response.json().catch(() => ({}));
  return NextResponse.json(body, { status: response.status });
}
