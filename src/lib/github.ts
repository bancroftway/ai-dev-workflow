import "server-only";
import { Octokit } from "octokit";
import { getServerAuthToken } from "@/auth";
import { E2E_GITHUB_TOKEN, E2E_MODE, githubAccessToken } from "@/lib/e2e";

/**
 * Server-only Octokit wrapper (architecture plan Section A.1). Never import this from a client
 * component -- the underlying access token grants `repo` scope on the signed-in user's GitHub
 * account. The token is read off the encrypted session JWT (getServerAuthToken), never off the
 * client-visible session object.
 */
/** Whether getOctokit() would succeed for the current request: a linked GitHub token on the
 * session, or the E2E-bypass PAT. Routes use this to answer 401 "github_not_connected" instead of
 * letting getOctokit throw. */
export async function githubConnected(): Promise<boolean> {
  return Boolean(githubAccessToken(await getServerAuthToken()));
}

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
