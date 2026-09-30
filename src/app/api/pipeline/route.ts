import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * The agent's pipeline descriptor (tabs, stages, gates, run order, modes) for client-only pages
 * with no server parent to fetch it for them (board, session lists). Session-independent, same
 * shape as agent/src/pipeline_layout.py's Pipeline.describe().
 */
export async function GET() {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ detail: "unauthenticated" }, { status: 401, headers: NO_STORE });
  }

  const response = await agentFetch("pipeline");
  if (!response.ok) {
    return NextResponse.json({ detail: "pipeline unavailable" }, { status: 502, headers: NO_STORE });
  }
  return NextResponse.json(await response.json(), { headers: NO_STORE });
}
