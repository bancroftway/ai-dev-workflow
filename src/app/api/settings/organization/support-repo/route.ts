import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken, isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Org-wide support-repo pointer (agent's /org-settings/support-repo) — where the support-issue
 * action files failed-run issues. Sibling of ../route.ts, gated the same way: Entra App Role
 * "Admin" server-side (see that file's comment). Separate from the provider/credential PUT so
 * saving this never re-probes a credential.
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
  const { support_repo } = (await request.json()) as { support_repo?: string | null };
  const response = await agentFetch("org-settings/support-repo", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      support_repo: typeof support_repo === "string" && support_repo.trim() ? support_repo.trim() : null,
      updated_by: updatedBy,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
