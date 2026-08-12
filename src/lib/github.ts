import "server-only";
import { Octokit } from "octokit";
import { auth } from "@/auth";

/**
 * Server-only Octokit wrapper (architecture plan Section A.1). Never import this from a client
 * component -- the underlying access token grants `repo` scope on the signed-in user's GitHub
 * account.
 */
export async function getOctokit(): Promise<Octokit> {
  const session = await auth();
  if (!session?.accessToken) {
    throw new Error("No GitHub access token on session -- caller must be authenticated");
  }
  return new Octokit({ auth: session.accessToken });
}
