import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";
import { getOctokit } from "@/lib/github";

// Must match entrypoint.sh's WORK_BRANCH / agent/src/git_ops.py's _WORK_BRANCH constant -- the
// pipeline's session index lives on this single repo-shared branch, never the user's own branch.
const WORK_BRANCH = "ai-dev-workflow";

/** One entry in `.ai-dev-workflow/sessions.json` -- schema owned by agent/src/session_index.py. */
export type SessionEntry = {
  run_id: string;
  thread_id: string;
  title: string;
  user: string;
  target_branch: string;
  started_at: string;
  ended_at: string | null;
  status: "in_progress" | "completed" | "failed" | "rejected" | "superseded";
  failure: { stage: string; type: string; message: string } | null;
  exit: { merge_ready: boolean; pr_title: string } | null;
};

type SessionIndexFile = {
  schema_version?: number;
  sessions?: unknown;
};

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * Session history for /select's per-repo history panel (GET ?owner=&repo=). Reads
 * .ai-dev-workflow/sessions.json straight off GitHub with the signed-in user's own token, so a
 * user who lacks repo access gets GitHub's own 404 rather than this route deciding on their
 * behalf what they can see.
 */
export async function GET(request: Request) {
  const session = await auth();
  const accessToken = session?.accessToken ?? (E2E_MODE ? E2E_GITHUB_TOKEN : undefined);
  if (!accessToken) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo query params are required" }, { status: 400 });
  }

  const octokit = await getOctokit();
  let content;
  try {
    content = await octokit.rest.repos.getContent({
      owner,
      repo,
      path: ".ai-dev-workflow/sessions.json",
      ref: WORK_BRANCH,
    });
  } catch (error) {
    // A 404 covers both "no session has ever run on this repo" (file absent) and "the
    // ai-dev-workflow branch itself doesn't exist yet" (branch absent) -- neither is an error,
    // both just mean there's no history to show.
    if ((error as { status?: number }).status === 404) {
      return NextResponse.json({ sessions: [] }, { headers: NO_STORE });
    }
    throw error;
  }

  if (Array.isArray(content.data) || content.data.type !== "file" || !content.data.content) {
    return NextResponse.json({ sessions: [] }, { headers: NO_STORE });
  }

  let sessions: SessionEntry[];
  try {
    const parsed = JSON.parse(
      Buffer.from(content.data.content, "base64").toString("utf-8"),
    ) as SessionIndexFile;
    if (!Array.isArray(parsed.sessions)) {
      throw new Error("sessions.json: `sessions` is not an array");
    }
    sessions = parsed.sessions as SessionEntry[];
  } catch {
    return NextResponse.json(
      { sessions: [], warning: "session index unreadable" },
      { headers: NO_STORE },
    );
  }

  const limit = Number(process.env.AIDW_RECENT_SESSIONS) || 20;
  const recent = [...sessions]
    .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""))
    .slice(0, limit);

  return NextResponse.json({ sessions: recent }, { headers: NO_STORE });
}
