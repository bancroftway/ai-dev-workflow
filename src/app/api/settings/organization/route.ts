import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";

/**
 * Org-wide coding-agent provider + credential proxy (agent's /org-settings). Two real differences
 * from the sibling vault/route.ts this was modeled on:
 *
 * - No entra_assertion forwarding: this isn't OBO-backed (Ruling 1 in the Part 4 plan) -- the org
 *   credential lives behind the agent's own standing Key Vault access, not a per-user delegation,
 *   so there's no assertion to forward and PUT doesn't need token.entraAccessToken at all.
 * - No repo-scoped authorization check: hasRepoAccess (src/lib/session-access.ts) is repo-scoped
 *   and doesn't apply -- org settings have no repo to scope to. Checked this codebase for any
 *   other org-level/admin concept (none exists anywhere -- every other authorization decision in
 *   this app is either "signed in at all" (src/proxy.ts, enforced globally) or hasRepoAccess), so
 *   the only real gate available today is that global sign-in check, already applied before this
 *   route handler ever runs. This means any signed-in user of this single-tenant internal tool can
 *   currently view/change the org-wide provider -- a real, intentionally-flagged gap, not an
 *   invented permission model standing in for one that doesn't exist yet.
 */

export async function GET() {
  // No per-user data in the request (unlike vault-config's owner/repo/user_login query) -- the
  // global sign-in gate in src/proxy.ts is the only check this needs, so this proxies straight
  // through exactly as-is.
  const response = await agentFetch("org-settings");
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  // updated_by is an audit-trail field: it must come from the server-side session, never from the
  // client body, the same real precedent vault/route.ts's PUT already sets for user_login (derived
  // from token.login there, never trusted from the request JSON). This context has no repo/GitHub
  // dependency, so token.login (GitHub handle, only present once a GitHub account is linked) isn't
  // the right first choice here -- an Entra-only admin who never linked GitHub must still be able
  // to save. Prefer the Entra profile fields every signed-in session actually carries (email, then
  // name), falling back to token.login, then to the immutable Entra object id so this is never
  // empty for anyone who actually made it past src/proxy.ts's sign-in gate.
  const updatedBy = token?.email ?? token?.name ?? token?.login ?? token?.oid;
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
