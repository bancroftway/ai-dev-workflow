import { NextResponse } from "next/server";
import { auditIdentity, getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { hasRepoAccess } from "@/lib/session-access";

/**
 * Project picker + "+ New Project" creation proxy (Part 3, New Ticket form) -- thin proxy to
 * sessions_api.py's projects_router, same pattern as ../settings/organization/route.ts (this
 * file's own template), POST derives `created_by` server-side and never trusts it from the client
 * body.
 *
 * GET used to be a plain unfiltered passthrough (project_store.list_projects() has no owner/repo
 * scoping of its own) -- confirmed a real leak (Task: Tickets View audit): any signed-in user
 * could see every project ever connected by anyone in the tenant (name, owner, repo), the exact
 * gap `hasRepoAccess` already closes for `/api/sessions/list`. Filtered here the same way, for a
 * project with a repo; a "+ New Project" row with no repo yet (owner/repo both null) has nothing
 * to check access against and is left in -- its creator already knows it exists, and nothing about
 * it names a real repository.
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
  if (!response.ok) {
    return NextResponse.json(await response.json(), { status: response.status });
  }
  const body = (await response.json()) as ProjectListResponse;
  const checks = await Promise.all(
    body.projects.map((p) => (p.owner && p.repo ? hasRepoAccess(p.owner, p.repo) : true)),
  );
  return NextResponse.json({ projects: body.projects.filter((_, i) => checks[i]) } satisfies ProjectListResponse);
}

export async function POST(request: Request) {
  const token = await getServerAuthToken();
  // created_by is an audit-trail field: derived server-side only, never trusted from the client
  // body -- same precedent as org-settings' route.ts's updated_by (field-preference rationale on
  // auditIdentity itself, src/auth.ts).
  const createdBy = auditIdentity(token);
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
