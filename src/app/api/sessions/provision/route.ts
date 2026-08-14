import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { E2E_GITHUB_ID, E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";
import { deriveThreadId } from "@/lib/workflow-thread";

const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";

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

  const threadId = deriveThreadId(owner, repo, branch, githubId);
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
