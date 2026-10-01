import { NextResponse } from "next/server";
import { agentFetch } from "@/lib/agent-client";
import { sandboxCredentials } from "@/lib/session-access";

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
 * Credentials (GitHub token, login, Entra assertion) come from sandboxCredentials -- shared with
 * the review route, which reconnects a sandbox an agent restart dropped.
 */
export async function POST(request: Request) {
  const credentials = await sandboxCredentials();
  if (!credentials) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { sessionId, projectId, owner, repo, branch, resume, confirmReopen, codeGenMode } = (await request.json()) as {
    sessionId?: string;
    // Which project (Part 3) this ticket belongs to. Optional here, not required: the agent's own
    // ProvisionRequest.project_id (Task 5) falls back to an already-existing session's own stored
    // project_id, so a resume (SessionHistory's Resume button) or a stale/bookmarked workflow URL
    // needs no projectId at all -- only a genuinely brand-new session does. The New Ticket form and
    // /select's own "start new session" action (SandboxSessionBoot.tsx, via the workflow page's
    // ?projectId= search param) both resolve and send a real one for that case.
    projectId?: string;
    owner?: string;
    repo?: string;
    branch?: string;
    resume?: boolean;
    // Root-caused 2026-09-12 ("rescue mechanism" work): Session Overview's recovery actions
    // (rewind-to-stage, reverify-metrics-exit, targeted-fix) already confirm reopening a finished
    // session via POST /api/sessions/actions before calling this route -- this just carries that
    // same confirmation through to the agent's own `is_finished_with_verdict` 409, which used to
    // refuse a resume-provision for exactly the sessions those actions exist to act on.
    confirmReopen?: boolean;
    // Task 1 (backend mode threading, plan Part 2): "yolo" / "draft_verify" / "mission_critical".
    // Optional here, not required -- like projectId above, only a genuinely-new session's frontend
    // call (Task 3's popup, a later task) sends one at all; a resume or an incidental reprovision
    // of an already-created session omits it and the agent falls back to this session's own
    // stored value (see agent/src/sessions_api.py's ProvisionRequest.code_gen_mode).
    codeGenMode?: string;
  };
  if (!sessionId || !owner || !repo || !branch) {
    return NextResponse.json(
      { error: "sessionId, owner, repo, and branch are required" },
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
      ...credentials,
      resume: Boolean(resume),
      confirm_reopen: Boolean(confirmReopen),
      code_gen_mode: codeGenMode,
    }),
  });

  const body = await response.json();
  return NextResponse.json(body, { status: response.status });
}
