import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";

/**
 * Project picker + "+ New Project" creation proxy (Part 3, New Ticket form) -- thin proxy to
 * sessions_api.py's projects_router, same pattern as ../settings/organization/route.ts (this
 * file's own template): GET is a plain passthrough (no per-user data, same authorization note as
 * that route -- this single-tenant tool has no admin/role concept, every signed-in user sees every
 * project), POST derives `created_by` server-side and never trusts it from the client body.
 */

/** Mirrors sessions_api.py's ProjectResponse -- owner/repo/tech_stack_id/tech_stack_text are all
 * nullable: a "+ New Project" row starts with owner/repo NULL until a ticket's own provisioning
 * scaffolds a repo for it; a Connect-Repository row (Task 5) starts with tech_stack_id/
 * tech_stack_text NULL instead. default_branch (Task 5) is the repo's real GitHub default branch,
 * set at connect time -- null for a not-yet-connected/scaffolded project or a pre-migration row,
 * in which case callers fall back to "main". */
export interface ProjectSummary {
  project_id: string;
  name: string;
  owner: string | null;
  repo: string | null;
  tech_stack_id: string | null;
  tech_stack_text: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  default_branch: string | null;
}

export interface ProjectListResponse {
  projects: ProjectSummary[];
}

export async function GET() {
  const response = await agentFetch("projects");
  return NextResponse.json(await response.json(), { status: response.status });
}

export async function POST(request: Request) {
  const token = await getServerAuthToken();
  // created_by is an audit-trail field: derived server-side only, never trusted from the client
  // body -- same precedent and same field-preference order as org-settings' route.ts's updated_by
  // (Entra profile fields every signed-in session carries, falling back to the GitHub login, then
  // the immutable Entra object id).
  const createdBy = token?.email ?? token?.name ?? token?.login ?? token?.oid;
  if (!createdBy) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  const { name, tech_stack_id, tech_stack_text } = (await request.json()) as {
    name?: string;
    tech_stack_id?: string | null;
    tech_stack_text?: string | null;
  };
  if (!name || !name.trim()) {
    return NextResponse.json({ detail: "name is required" }, { status: 400 });
  }

  const response = await agentFetch("projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: name.trim(),
      tech_stack_id: tech_stack_id ?? null,
      tech_stack_text: tech_stack_text ?? null,
      created_by: createdBy,
    }),
  });
  return NextResponse.json(await response.json(), { status: response.status });
}
