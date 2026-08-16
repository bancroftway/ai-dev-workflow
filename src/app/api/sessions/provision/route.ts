import { NextResponse } from "next/server";
import type { Octokit } from "octokit";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";
import { getOctokit } from "@/lib/github";
import { deriveThreadId } from "@/lib/workflow-thread";

const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";
// Must match entrypoint.sh's WORK_BRANCH / agent/src/git_ops.py's _WORK_BRANCH constant.
const WORK_BRANCH = "ai-dev-workflow";

/** One entry in `.ai-dev-workflow/sessions.json` -- schema owned by a later task (session
 * history/resume); only the fields the provision guard below needs are typed here. */
type SessionIndexEntry = {
  status?: string;
  user?: string;
};

/**
 * Hard provision guard (WS0 migration, BLOCKER fix): with one ai-dev-workflow branch shared by
 * every session on a repo, two users provisioning at once would silently share it. Returns the
 * first still-in_progress entry started by someone else, or null when there's nothing to guard
 * against -- including the common case today where the branch/file doesn't exist yet (a 404,
 * since sessions.json is created by a later task).
 */
async function findConflictingSession(
  octokit: Octokit,
  owner: string,
  repo: string,
  currentLogin: string,
): Promise<SessionIndexEntry | null> {
  let content;
  try {
    content = await octokit.rest.repos.getContent({
      owner,
      repo,
      path: ".ai-dev-workflow/sessions.json",
      ref: WORK_BRANCH,
    });
  } catch (error) {
    if ((error as { status?: number }).status === 404) return null;
    throw error;
  }
  if (Array.isArray(content.data) || content.data.type !== "file" || !content.data.content) {
    return null;
  }
  try {
    const parsed = JSON.parse(Buffer.from(content.data.content, "base64").toString("utf-8")) as {
      sessions?: SessionIndexEntry[];
    };
    return (
      parsed.sessions?.find(
        (entry) => entry.status === "in_progress" && entry.user && entry.user !== currentLogin,
      ) ?? null
    );
  } catch {
    return null; // malformed sessions.json -- fail open, the writer's schema isn't ours to enforce
  }
}

/**
 * Server-to-server proxy into the agent's sandbox provisioning endpoint (architecture plan
 * Section C.4). The browser never holds or sends the GitHub access token -- this route reads it
 * from the server-side session and forwards it, and re-derives threadId itself from the session
 * rather than trusting a client-supplied value, since threadId keys the sandbox registry.
 *
 * E2E mode: the forwarded token is what the sandbox clones the target repo with, so the fallback
 * PAT must have `repo` read on that repo; githubId falls back to a fixed synthetic id because it
 * keys deriveThreadId.
 */
export async function POST(request: Request) {
  const session = await auth();
  const accessToken = session?.accessToken ?? (E2E_MODE ? E2E_GITHUB_TOKEN : undefined);
  const githubId = session?.githubId ?? (E2E_MODE ? E2E_GITHUB_ID : undefined);
  if (!accessToken || !githubId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { owner, repo, branch } = (await request.json()) as {
    owner?: string;
    repo?: string;
    branch?: string;
  };
  if (!owner || !repo || !branch) {
    return NextResponse.json({ error: "owner, repo, and branch are required" }, { status: 400 });
  }

  const octokit = await getOctokit();
  const { login: currentLogin } = (await octokit.rest.users.getAuthenticated()).data;
  const conflict = await findConflictingSession(octokit, owner, repo, currentLogin);
  if (conflict) {
    return NextResponse.json(
      {
        error:
          `Another session is already in progress on this repo (started by ${conflict.user}). ` +
          "Only one active session per repo is allowed -- wait for it to finish or ask them to close it.",
      },
      { status: 409 },
    );
  }

  const threadId = deriveThreadId(owner, repo, githubId);
  const agentBaseUrl = AGENT_URL.endsWith("/") ? AGENT_URL : `${AGENT_URL}/`;

  const response = await fetch(new URL("sessions/provision", agentBaseUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: threadId,
      owner,
      repo,
      branch,
      github_token: accessToken,
    }),
  });

  const body = await response.json();
  return NextResponse.json(body, { status: response.status });
}
