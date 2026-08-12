import { NextResponse } from "next/server";
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
