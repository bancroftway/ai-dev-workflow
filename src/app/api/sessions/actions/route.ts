import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { lookupSessionWithAuthorization } from "@/lib/session-access";

/**
 * On-demand session actions ("Refresh Key Vault secrets" in the workspace header;
 * "confirm-reopen" from RequirementsView's post-completion confirm prompt). Named actions only --
 * the agent's dispatch validates the action name; nothing here or there ever forwards shell.
 * Authorization is the app's standard repo-access check (session-access.ts): anyone who can see
 * the repo can act on its session, same as they could resume it.
 */
const KNOWN_ACTIONS = ["refresh-secrets", "confirm-reopen"] as const;

export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const { sessionId, action } = (await request.json()) as { sessionId?: string; action?: string };
  if (!sessionId || !KNOWN_ACTIONS.includes(action as (typeof KNOWN_ACTIONS)[number])) {
    return NextResponse.json({ detail: "sessionId and a known action are required" }, { status: 400 });
  }

  const lookup = await lookupSessionWithAuthorization(sessionId);
  if (lookup.kind !== "authorized") {
    // Same shape for not_found and denied -- never confirm a session's existence to a caller
    // who can't see it (mirrors session-access.ts's own contract).
    return NextResponse.json({ detail: "session not found" }, { status: 404 });
  }
  // Only refresh-secrets needs a fresh Entra assertion to read the vault -- confirm-reopen is a
  // plain in-process flag set on the agent, nothing to authenticate against Azure with.
  if (action === "refresh-secrets" && !token?.entraAccessToken) {
    return NextResponse.json(
      { detail: "No Microsoft session -- sign in again to refresh secrets" },
      { status: 401 },
    );
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, entra_assertion: token?.entraAccessToken ?? "" }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
