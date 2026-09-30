import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken, isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Single Org Settings ("Advanced") field write/reset (agent's /runtime-settings/{name}). Sibling
 * of ../route.ts (the list), gated the same way. `updated_by` is always server-derived from the
 * signed-in session, same audit-trail rule ../../organization/route.ts's PUT already follows —
 * never trusted from the client body/query.
 */
export async function PUT(request: Request, { params }: { params: Promise<{ name: string }> }) {
  if (!E2E_MODE && !(await isAdminRequest())) {
    return NextResponse.json({ detail: "Admin role required" }, { status: 403 });
  }
  const token = await getServerAuthToken();
  const updatedBy = auditIdentity(token);
  if (!updatedBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { name } = await params;
  const { value } = (await request.json()) as { value?: unknown };
  const response = await agentFetch(`runtime-settings/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, updated_by: updatedBy }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function DELETE(_request: Request, { params }: { params: Promise<{ name: string }> }) {
  if (!E2E_MODE && !(await isAdminRequest())) {
    return NextResponse.json({ detail: "Admin role required" }, { status: 403 });
  }
  const token = await getServerAuthToken();
  const updatedBy = auditIdentity(token);
  if (!updatedBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }
  const { name } = await params;
  const response = await agentFetch(
    `runtime-settings/${encodeURIComponent(name)}?updated_by=${encodeURIComponent(updatedBy)}`,
    { method: "DELETE" },
  );
  return NextResponse.json(await response.json(), { status: response.status });
}
