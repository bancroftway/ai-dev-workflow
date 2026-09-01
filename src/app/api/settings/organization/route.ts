import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken, isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Org-wide coding-agent provider + credential proxy (agent's /org-settings). Two real differences
 * from the sibling vault/route.ts this was modeled on:
 *
 * - No entra_assertion forwarding: this isn't OBO-backed (Ruling 1 in the Part 4 plan) -- the org
 *   credential lives behind the agent's own standing Key Vault access, not a per-user delegation,
 *   so there's no assertion to forward and PUT doesn't need token.entraAccessToken at all.
 * - Authorization: the Entra App Role "Admin" (isAdminRequest, src/auth.ts) -- this closed the
 *   long-flagged "any signed-in user can change org settings" gap (CI/CD plan Phase 6). The check
 *   is server-side on the JWT's roles claim; the UI's hidden link/404 page are courtesy only.
 *   E2E bypass mirrors the codebase-wide convention (src/lib/e2e.ts, non-prod only).
 */

async function forbidNonAdmin(): Promise<NextResponse | null> {
  if (E2E_MODE || (await isAdminRequest())) return null;
  return NextResponse.json({ detail: "Admin role required" }, { status: 403 });
}

export async function GET() {
  const forbidden = await forbidNonAdmin();
  if (forbidden) return forbidden;
  const response = await agentFetch("org-settings");
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const forbidden = await forbidNonAdmin();
  if (forbidden) return forbidden;
  const token = await getServerAuthToken();
  // updated_by is an audit-trail field: it must come from the server-side session, never from the
  // client body, the same real precedent vault/route.ts's PUT already sets for user_login (derived
  // from token.login there, never trusted from the request JSON). Field-preference rationale lives
  // on auditIdentity itself (src/auth.ts).
  const updatedBy = auditIdentity(token);
  if (!updatedBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const { provider, credential, credential_kind } = (await request.json()) as {
    provider?: string;
    credential?: string | null;
    // C-1 (whole-branch review): which Claude billing mode `credential` is ("api_key" | "oauth").
    // Irrelevant for copilot -- forwarded through as-is either way, the agent's own
    // OrgSettingsPutRequest.credential_kind ignores it for a non-claude provider.
    credential_kind?: string | null;
  };
  if (provider !== "copilot" && provider !== "claude") {
    return NextResponse.json({ detail: 'provider must be "copilot" or "claude"' }, { status: 400 });
  }

  const response = await agentFetch("org-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider,
      // Omitted/blank means "keep whatever's already saved" -- matches the agent's own
      // OrgSettingsPutRequest.credential: str | None = None contract.
      credential: typeof credential === "string" && credential.trim() ? credential.trim() : null,
      credential_kind: credential_kind === "oauth" || credential_kind === "api_key" ? credential_kind : null,
      updated_by: updatedBy,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
