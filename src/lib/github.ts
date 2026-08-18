import "server-only";
import { Octokit } from "octokit";
import { getServerAuthToken } from "@/auth";
import { E2E_GITHUB_TOKEN, E2E_MODE } from "@/lib/e2e";

/**
 * Server-only Octokit wrapper (architecture plan Section A.1). Never import this from a client
 * component -- the underlying access token grants `repo` scope on the signed-in user's GitHub
 * account. The token is read off the encrypted session JWT (getServerAuthToken), never off the
 * client-visible session object.
 */
export async function getOctokit(): Promise<Octokit> {
  const token = await getServerAuthToken();
  if (!token?.accessToken) {
    if (E2E_MODE && E2E_GITHUB_TOKEN) {
      return new Octokit({ auth: E2E_GITHUB_TOKEN });
    }
    throw new Error("No linked GitHub account -- caller must connect GitHub first");
  }
  return new Octokit({ auth: token.accessToken });
}
