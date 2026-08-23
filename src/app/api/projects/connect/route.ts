import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";

/**
 * Connect-a-Repository proxy (Task 5's own form calls this; built now alongside its sibling
 * ../route.ts since both are thin proxies onto the same Part 3 projects_router). Same
 * server-derived created_by pattern as ../route.ts's POST and ../../settings/organization/route.ts
 * before it -- never trusted from the client body.
 */
export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const createdBy = token?.email ?? token?.name ?? token?.login ?? token?.oid;
  if (!createdBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const { owner, repo } = (await request.json()) as { owner?: string; repo?: string };
  if (!owner || !repo) {
    return NextResponse.json({ detail: "owner and repo are required" }, { status: 400 });
  }

  const response = await agentFetch("projects/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner, repo, created_by: createdBy }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
