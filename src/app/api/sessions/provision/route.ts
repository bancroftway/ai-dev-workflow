import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { agentFetch } from "@/lib/agent-client";
import { E2E_GITHUB_ID, E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";

/**
 * Server-to-server proxy into the agent's sandbox provisioning endpoint (architecture plan
 * Section C.4). The browser never holds or sends the GitHub access token -- this route reads it
 * from the server-side session and forwards it.
 *
 * `sessionId` is caller-supplied, not derived: a new session's id is a UUID minted client-side
 * (src/app/select/page.tsx's "Start new session" button); a resume passes back the exact
 * historical session id being resumed. There is no more deterministic (owner, repo, user) ->
 * session id formula (branch-per-session removed the single shared work branch that made one
 * possible) -- the agent enforces resume rules server-side (404/409) regardless of what's sent
 * here, so this route does no session-existence checking of its own. Concurrency is fully open:
 * any number of sessions can be in-progress on the same repo at once, each on its own branch.
 *
 * E2E mode: the forwarded token is what the sandbox clones the target repo with, so the fallback
 * PAT must have `repo` read on that repo.
 */
export async function POST(request: Request) {
  const token = await getServerAuthToken();
  const accessToken = token?.accessToken ?? (E2E_MODE ? E2E_GITHUB_TOKEN : undefined);
  const githubId = token?.githubId ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  const userLogin = token?.login ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!accessToken || !githubId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId, projectId, owner, repo, branch, resume } = (await request.json()) as {
    sessionId?: string;
    // Which project (Part 3) this ticket belongs to -- required by the agent's own
    // ProvisionRequest.project_id (no default there), so required here too rather than passing
    // through to a less-clear 422 from the agent. The New Ticket form (src/app/(boxed)/tickets/new)
    // is the only caller that currently sends this; SandboxSessionBoot.tsx's own call (the plain
    // /select repo/branch flow) does not yet, which is a known pre-existing gap this route does not
    // paper over -- see task-4-report.md.
    projectId?: string;
    owner?: string;
    repo?: string;
    branch?: string;
    resume?: boolean;
  };
  if (!sessionId || !projectId || !owner || !repo || !branch) {
    return NextResponse.json(
      { error: "sessionId, projectId, owner, repo, and branch are required" },
      { status: 400 },
    );
  }

  const response = await agentFetch("sessions/provision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: sessionId,
      project_id: projectId,
      owner,
      repo,
      branch,
      github_token: accessToken,
      // Advisory only -- see session_store.py's module docstring.
      user_login: userLogin ?? "",
      resume: Boolean(resume),
      // Fresh Entra access token (the jwt callback refreshes it before this route reads it) --
      // the agent exchanges it on-behalf-of for the session's Key Vault secrets at provision
      // time, then discards it. Absent in E2E-bypass mode; the agent skips the vault fetch then.
      entra_assertion: token?.entraAccessToken ?? null,
    }),
  });

  const body = await response.json();
  return NextResponse.json(body, { status: response.status });
}
