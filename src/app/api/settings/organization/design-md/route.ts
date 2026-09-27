import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken, isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Deployment-wide default DESIGN.md (agent's /org-settings/design-md) — the fallback a repo uses
 * when it has no override of its own (../../../repos/design-md/route.ts). Sibling of
 * ../support-repo/route.ts: same Entra "Admin" gate, its own endpoint so saving this never
 * re-probes a provider credential.
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
  const { design_md } = (await request.json()) as { design_md?: string | null };
  const response = await agentFetch("org-settings/design-md", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      design_md: typeof design_md === "string" && design_md.trim() ? design_md : null,
      updated_by: updatedBy,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
