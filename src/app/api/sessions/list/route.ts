import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_MODE } from "@/lib/e2e";
import type { Session } from "@/lib/session-types";
import { hasRepoAccess } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * Session list for /select's session-list panel (GET ?owner=&repo=&source_branch=). Proxies the
 * agent's `GET /sessions` -- SQL Server (session_store.py) is the single source of truth now, not
 * a `.ai-dev-workflow/sessions.json` file read off GitHub.
 *
 * `auth()` alone only proves SOME user is signed in, not that THIS user can see THIS owner/repo --
 * without the hasRepoAccess check below, any authenticated user could page through arbitrary
 * owner/repo query params and read another team's session titles, failure messages, and PR links.
 * Returns an empty list (not a 403/404) on no access, matching this route's existing "nothing to
 * show" shape for the "never run here" case -- doesn't reveal whether the repo exists either way.
 */
export async function GET(request: Request) {
  const session = await auth();
  if (!session && !E2E_MODE) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  const sourceBranch = searchParams.get("source_branch");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo query params are required" }, { status: 400 });
  }

  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ sessions: [] }, { headers: NO_STORE });
  }

  const agentParams = new URLSearchParams({ owner, repo });
  if (sourceBranch) agentParams.set("source_branch", sourceBranch);

  const response = await agentFetch(`sessions?${agentParams}`);
  if (!response.ok) {
    return NextResponse.json({ sessions: [] }, { headers: NO_STORE });
  }
  const body = (await response.json()) as { sessions: Session[] };
  return NextResponse.json(body, { headers: NO_STORE });
}
