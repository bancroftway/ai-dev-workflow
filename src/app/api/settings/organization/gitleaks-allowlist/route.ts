import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken, isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Org-wide extra gitleaks allowlist entries (agent's /org-settings/gitleaks-allowlist) -- lets an
 * admin suppress known-false-positive secret findings (e.g. e2e/smoke-test fixture values)
 * without touching every scanned repo's own git history. Sibling of ../design-md/route.ts: same
 * Entra "Admin" gate, its own endpoint so saving this never re-probes a provider credential.
 */
export async function PUT(request: Request) {
  if (!E2E_MODE && !(await isAdminRequest())) {
    return NextResponse.json({ detail: "Admin role required" }, { status: 403 });
  }
  const token = await getServerAuthToken();
  const updatedBy = auditIdentity(token);
  if (!updatedBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { gitleaks_extra_stopwords, gitleaks_extra_allow_paths } = (await request.json()) as {
    gitleaks_extra_stopwords?: string | null;
    gitleaks_extra_allow_paths?: string | null;
  };
  const response = await agentFetch("org-settings/gitleaks-allowlist", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      gitleaks_extra_stopwords:
        typeof gitleaks_extra_stopwords === "string" && gitleaks_extra_stopwords.trim() ? gitleaks_extra_stopwords : null,
      gitleaks_extra_allow_paths:
        typeof gitleaks_extra_allow_paths === "string" && gitleaks_extra_allow_paths.trim() ? gitleaks_extra_allow_paths : null,
      updated_by: updatedBy,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
