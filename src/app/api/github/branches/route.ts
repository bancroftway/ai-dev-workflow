import { NextResponse } from "next/server";
import { getOctokit, githubConnected } from "@/lib/github";

export interface BranchSummary {
  name: string;
}

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  if (!owner || !repo) {
    return NextResponse.json({ error: "owner and repo query params are required" }, { status: 400 });
  }

  if (!(await githubConnected())) {
    return NextResponse.json({ error: "github_not_connected" }, { status: 401 });
  }

  const octokit = await getOctokit();
  const branches = await octokit.paginate(octokit.rest.repos.listBranches, {
    owner,
    repo,
    per_page: 100,
  });

  const summaries: BranchSummary[] = branches.map((b) => ({ name: b.name }));
  return NextResponse.json({ branches: summaries });
}
