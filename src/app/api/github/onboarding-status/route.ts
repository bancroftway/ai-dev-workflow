import { NextResponse } from "next/server";
import { getOctokit } from "@/lib/github";

/**
 * Onboarding detection (architecture plan Section A.2): presence of `.ai-dev-workflow/` on the
 * given branch means this repo/branch has been used with this tool before. A 200 from the
 * Contents API is enough to answer that -- no clone needed just to show the picker's badge.
 */
export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const owner = searchParams.get("owner");
  const repo = searchParams.get("repo");
  const branch = searchParams.get("branch");
  if (!owner || !repo || !branch) {
    return NextResponse.json(
      { error: "owner, repo, and branch query params are required" },
      { status: 400 },
    );
  }

  const octokit = await getOctokit();
  // The pipeline commits on a tool-owned work branch (ai-dev-workflow/<branch>), never on the
  // user's selected branch -- check the work branch first, fall back to the selected branch for
  // repos onboarded before work branches existed (or where the work branch was merged in).
  for (const ref of [`ai-dev-workflow/${branch}`, branch]) {
    try {
      await octokit.rest.repos.getContent({ owner, repo, path: ".ai-dev-workflow", ref });
      return NextResponse.json({ onboarded: true });
    } catch (error) {
      const status = (error as { status?: number }).status;
      if (status !== 404) {
        throw error;
      }
    }
  }
  return NextResponse.json({ onboarded: false });
}
