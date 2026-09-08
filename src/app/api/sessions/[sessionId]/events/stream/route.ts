import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { getAuthorizedSession, isAuthenticated } from "@/lib/session-access";

/**
 * SSE passthrough for `[sessionId]/events/route.ts` right next to this file -- same auth/
 * ownership gate as that route (and the same "checked once at connect" caveat -- see
 * `[sessionId]/stream/route.ts`'s own comment), but streams durable dbo.run_events rows as they
 * land instead of the frontend polling this same data every 15s.
 *
 * `response.body` passed straight through, status/content-type forwarded as-is -- see
 * `[sessionId]/stream/route.ts` for why this is safe for the agent's error responses too.
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

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/events/stream`);
  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "text/event-stream",
      "Cache-Control": "no-cache",
    },
  });
}
