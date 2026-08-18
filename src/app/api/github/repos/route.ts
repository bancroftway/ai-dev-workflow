import { NextResponse } from "next/server";
import { getServerAuthToken } from "@/auth";
import { E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";
import { getOctokit } from "@/lib/github";

export interface RepoSummary {
  owner: string;
  repo: string;
  fullName: string;
  private: boolean;
  defaultBranch: string;
  updatedAt: string | null;
}

export async function GET() {
  // Distinguished from a real failure (network/GitHub-API error) so the picker can show a quiet
  // "connect GitHub" state instead of a red error box -- the settings banner already prompts for
  // this, a 500-styled error line would just be redundant alarm.
  const token = await getServerAuthToken();
  if (!token?.accessToken && !(E2E_MODE && E2E_GITHUB_TOKEN)) {
    return NextResponse.json({ error: "github_not_connected" }, { status: 401 });
  }

  const octokit = await getOctokit();
  const repos = await octokit.paginate(octokit.rest.repos.listForAuthenticatedUser, {
    affiliation: "owner,collaborator,organization_member",
    sort: "updated",
    per_page: 100,
  });

  const summaries: RepoSummary[] = repos.map((r) => ({
    owner: r.owner.login,
    repo: r.name,
    fullName: r.full_name,
    private: r.private,
    defaultBranch: r.default_branch ?? "main",
    updatedAt: r.updated_at ?? null,
  }));

  return NextResponse.json({ repos: summaries });
}
