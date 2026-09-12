import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { requireAuthorizedSession } from "@/lib/session-access";

/**
 * On-demand session actions ("Refresh Key Vault secrets" in the workspace header;
 * "confirm-reopen" from RequirementsView's post-completion confirm prompt; "rewind-to-stage" from
 * SessionOverview's per-stage restart button; "targeted-fix" (root-caused 2026-09-12, "a way to
 * remedy without starting over") from SessionOverview's "Fix these findings" action -- seeds a
 * scoped agent fix pass from the prior run's own blocking_reasons, no stage_key needed). Named
 * actions only -- the agent's dispatch validates the action name; nothing here or there ever
 * forwards shell. Authorization is the app's standard repo-access check (session-access.ts):
 * anyone who can see the repo can act on its session, same as they could resume it.
 */
const KNOWN_ACTIONS = ["refresh-secrets", "confirm-reopen", "rewind-to-stage", "targeted-fix"] as const;

export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const { sessionId, action, stageKey } = (await request.json()) as {
    sessionId?: string;
    action?: string;
    stageKey?: string;
  };
  if (!sessionId || !KNOWN_ACTIONS.includes(action as (typeof KNOWN_ACTIONS)[number])) {
    return NextResponse.json({ detail: "sessionId and a known action are required" }, { status: 400 });
  }

  const lookup = await requireAuthorizedSession(sessionId);
  if (lookup instanceof NextResponse) return lookup;
  // Only refresh-secrets needs a fresh Entra assertion to read the vault -- confirm-reopen and
  // rewind-to-stage are both plain in-process flags set on the agent, nothing to authenticate
  // against Azure with.
  if (action === "refresh-secrets" && !token?.entraAccessToken) {
    return NextResponse.json(
      { detail: "No Microsoft session -- sign in again to refresh secrets" },
      { status: 401 },
    );
  }

  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      entra_assertion: token?.entraAccessToken ?? "",
      ...(action === "rewind-to-stage" ? { stage_key: stageKey } : {}),
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
