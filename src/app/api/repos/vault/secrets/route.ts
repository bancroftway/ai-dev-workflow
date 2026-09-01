import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";

/**
 * Lists the vault's secret NAMES (never values) plus the saved selection, for the settings page's
 * picker. POST because the OBO assertion rides in the body -- a bearer credential does not belong
 * in a URL or query log.
 */
export async function POST(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  if (!token.entraAccessToken) {
    return NextResponse.json(
      { detail: "No Microsoft session -- sign in again to list vault secrets" },
      { status: 401 },
    );
  }
  const { owner, repo } = (await request.json()) as { owner?: string; repo?: string };
  if (!owner || !repo) {
    return NextResponse.json({ detail: "owner and repo are required" }, { status: 400 });
  }
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }
  const response = await agentFetch("vault-config/secrets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner,
      repo,
      user_login: token.login,
      entra_assertion: token.entraAccessToken,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
