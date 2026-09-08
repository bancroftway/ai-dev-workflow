import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

/**
 * SSE passthrough for `[sessionId]/route.ts` right next to this file -- same auth/ownership gate
 * (checked once, here, at connect -- not re-checked for the life of the connection, which is why
 * the agent's own stream_session_row enforces a max connection duration), but streams instead of
 * buffering to JSON so the frontend gets pushed updates instead of polling every 10s.
 *
 * `response.body` is passed straight through as this Response's body -- both are Fetch API
 * ReadableStreams, so no buffering/re-encoding happens in this hop. Status and content-type are
 * forwarded as-is: a 404 (unknown session) or 401 (bad shared secret) from the agent arrives here
 * as ordinary JSON, not a stream, and passes through exactly the same way.
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

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/stream`);
  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
