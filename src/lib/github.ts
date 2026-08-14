import "server-only";
import { Octokit } from "octokit";
import { auth } from "@/auth";
import { E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";

/**
 * Server-only Octokit wrapper (architecture plan Section A.1). Never import this from a client
 * component -- the underlying access token grants `repo` scope on the signed-in user's GitHub
 * account.
 */
export async function getOctokit(): Promise<Octokit> {
  const session = await auth();
  if (!session?.accessToken) {
    if (E2E_MODE && E2E_GITHUB_TOKEN) {
      return new Octokit({ auth: E2E_GITHUB_TOKEN });
    }
    throw new Error("No GitHub access token on session -- caller must be authenticated");
  }
  return new Octokit({ auth: session.accessToken });
}
