import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";

/**
 * Org-wide support-repo pointer (agent's /org-settings/support-repo) — where the support-issue
 * action files failed-run issues. Sibling of ../route.ts and under its same flagged authorization
 * gap: any signed-in user can change it (no org-admin concept exists; see that file's comment).
 * Separate from the provider/credential PUT so saving this never re-probes a credential.
 */
export async function PUT(request: Request) {
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
