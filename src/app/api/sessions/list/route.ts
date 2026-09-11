import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import type { Session } from "@/lib/session-types";
import { hasRepoAccess, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * Session list for /select's session-list panel (GET ?owner=&repo=&source_branch=) and (Task 9)
 * the project-scoped Board (GET ?owner=&repo=&project_id=). Proxies the agent's `GET /sessions` --
 * SQL Server (session_store.py) is the single source of truth now, not a
 * `.ai-dev-workflow/sessions.json` file read off GitHub.
 *
 * `auth()` alone only proves SOME user is signed in, not that THIS user can see THIS owner/repo --
 * without the hasRepoAccess check below, any authenticated user could page through arbitrary
 * owner/repo query params and read another team's session titles, failure messages, and PR links.
 * Returns an empty list (not a 403/404) on no access, matching this route's existing "nothing to
 * show" shape for the "never run here" case -- doesn't reveal whether the repo exists either way.
 */
export async function GET(request: Request) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  const sourceBranch = searchParams.get("source_branch");
  const projectId = searchParams.get("project_id");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo query params are required" }, { status: 400 });
  }

  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ sessions: [] }, { headers: NO_STORE });
  }

  const agentParams = new URLSearchParams({ owner, repo });
  if (sourceBranch) agentParams.set("source_branch", sourceBranch);
  if (projectId) agentParams.set("project_id", projectId);

  const response = await agentFetch(`sessions?${agentParams}`);
  if (!response.ok) {
    // Root-caused 2026-09-11: this used to fake a successful empty list here, indistinguishable
    // from "this repo genuinely has zero sessions" -- a dead/unreachable agent (agentFetch's own
    // synthetic 502) then read as data loss instead of a backend outage. Forward the real status
    // and detail; SessionHistory.tsx already throws on a non-ok response and renders it as an
    // error (not a false "no sessions yet"), it just never received one before this.
    const body = await response.json().catch(() => ({ detail: `agent request failed (${response.status})` }));
    return NextResponse.json(body, { status: response.status, headers: NO_STORE });
  }
  const body = (await response.json()) as { sessions: Session[] };
  return NextResponse.json(body, { headers: NO_STORE });
}
