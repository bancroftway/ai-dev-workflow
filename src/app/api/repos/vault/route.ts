import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";
import { E2E_GITHUB_ID, E2E_MODE } from "@/lib/e2e";

/**
 * Per user-repo Key Vault mapping proxy (agent's /vault-config). The browser never sees the
 * Entra access token: PUT reads it off the server-side session JWT and forwards it as the OBO
 * assertion the agent test-reads the vault with -- a save only succeeds if the vault is actually
 * readable AS THIS USER, so the stored mapping is proven-good at write time.
 */
const VAULT_URI_RE = /^https:\/\/[a-z0-9][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net\/?$/;

export async function GET(request: Request) {
  const token = await getServerAuthToken();
  const login = token?.login ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo are required" }, { status: 400 });
  }
  const params = new URLSearchParams({ owner, repo, user_login: login });
  const response = await agentFetch(`vault-config?${params}`);
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function PUT(request: Request) {
  const token = await getServerAuthToken();
  if (!token?.login) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  if (!token.entraAccessToken) {
    return NextResponse.json(
      { detail: "No Microsoft session -- sign in again to configure a Key Vault" },
      { status: 401 },
    );
  }
  const { owner, repo, vaultUri } = (await request.json()) as {
    owner?: string;
    repo?: string;
    vaultUri?: string;
  };
  if (!owner || !repo || !vaultUri) {
    return NextResponse.json({ detail: "owner, repo, and vaultUri are required" }, { status: 400 });
  }
  if (!VAULT_URI_RE.test(vaultUri.trim())) {
    return NextResponse.json(
      { detail: "vaultUri must look like https://<name>.vault.azure.net/" },
      { status: 422 },
    );
  }
  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ detail: "You do not have access to this repository" }, { status: 403 });
  }

  const response = await agentFetch("vault-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      owner,
      repo,
      user_login: token.login,
      vault_uri: vaultUri.trim(),
      entra_assertion: token.entraAccessToken,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
