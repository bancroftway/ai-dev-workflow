import { NextResponse } from "next/server";
import { isAdminRequest } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";

/**
 * Org Settings ("Advanced") list proxy (agent's /runtime-settings) — every migrated config.py
 * setting's metadata + current resolved value + override status. Gated the same way
 * ../organization/route.ts is (Entra App Role "Admin", server-side); this list is read-only, no
 * updated_by needed. Per-setting writes/resets go through [name]/route.ts.
 */
export async function GET() {
  if (!E2E_MODE && !(await isAdminRequest())) {
    return NextResponse.json({ detail: "Admin role required" }, { status: 403 });
  }
  const response = await agentFetch("runtime-settings");
  return NextResponse.json(await response.json(), { status: response.status });
}
