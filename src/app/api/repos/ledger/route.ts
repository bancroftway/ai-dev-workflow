import "server-only";
import { NextResponse } from "next/server";
import { getOctokit } from "@/lib/github";
import { hasRepoAccess, isAuthenticated } from "@/lib/session-access";

const NO_STORE = { "Cache-Control": "no-store" };

// Mirrors agent/src/spec_ledger.py's LEDGER_PATH exactly -- this route reads the same
// git-committed file the pipeline itself reads/writes, no separate copy.
const LEDGER_PATH = ".ai-dev-workflow/spec/ledger.json";

export interface LedgerEntry {
  id: string;
  kind: "user_story" | "acceptance_criterion";
  status: "active" | "retired" | "revised" | "deferred";
  title?: string;
  description?: string;
  parent_us_id?: string;
  source_ticket_id?: string;
  first_seen_run_id?: string;
  resolved_at?: string;
  resolved_run_id?: string;
}

export interface LedgerResponse {
  entries: LedgerEntry[];
}

/**
 * Repo-scoped Tickets page's Requirements section (Task: Tickets View, Scope §1) -- reads
 * `.ai-dev-workflow/spec/ledger.json` straight off GitHub at the repo's default branch, the same
 * elevated source of truth every ticket's specification/plan/ac-to-tests stages already read via
 * spec_ledger.load_ledger. No sandbox/container involved: this is a plain committed file, so a
 * live pipeline run isn't needed just to display it.
 */
export async function GET(request: Request) {
  if (!(await isAuthenticated())) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo query params are required" }, { status: 400 });
  }

  if (!(await hasRepoAccess(owner, repo))) {
    return NextResponse.json({ entries: [] }, { headers: NO_STORE });
  }

  const octokit = await getOctokit();
  try {
    const res = await octokit.rest.repos.getContent({ owner, repo, path: LEDGER_PATH });
    if (Array.isArray(res.data) || res.data.type !== "file" || !res.data.content) {
      return NextResponse.json({ entries: [] }, { headers: NO_STORE });
    }
    const raw = Buffer.from(res.data.content, "base64").toString("utf-8");
    const parsed = JSON.parse(raw) as { entries?: LedgerEntry[] };
    return NextResponse.json({ entries: parsed.entries ?? [] } satisfies LedgerResponse, { headers: NO_STORE });
  } catch (error) {
    // Not found (repo never onboarded, or no ledger yet) and any parse failure both mean "nothing
    // to show" -- neither is an error worth surfacing on this read-only display route.
    if ((error as { status?: number }).status === 404) {
      return NextResponse.json({ entries: [] }, { headers: NO_STORE });
    }
    return NextResponse.json({ entries: [] }, { headers: NO_STORE });
  }
}
