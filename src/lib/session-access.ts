import "server-only";
import { agentFetch } from "@/lib/agent-client";
import { getOctokit } from "@/lib/github";
import type { Session } from "@/lib/session-types";

/**
 * True iff the caller's own GitHub token can read (owner, repo) -- GitHub returns 404 for both
 * "doesn't exist" and "no access", which is also the response shape wanted here (never confirm a
 * repo's existence to a caller who can't see it). This is the app's existing access model (every
 * repo the user acts on elsewhere is one their own token can list) applied to session data too --
 * gated by repo access, not by which user happens to have started a given session, since
 * teammates who can see a repo are meant to see/resume each other's sessions on it.
 */
export async function hasRepoAccess(owner: string, repo: string): Promise<boolean> {
  const octokit = await getOctokit();
  try {
    await octokit.rest.repos.get({ owner, repo });
    return true;
  } catch (error) {
    if ((error as { status?: number }).status === 404) return false;
    throw error;
  }
}

export type SessionLookupResult =
  | { kind: "not_found" }
  | { kind: "denied" }
  | { kind: "authorized"; session: Session };

/**
 * Fetches a session by id, distinguishing "no such session" from "exists but you can't see it" --
 * the two must NOT collapse into one outcome for a caller (the workflow page) that treats
 * not-found as "proceed, this is a brand-new unprovisioned session." Collapsing them would let a
 * denied caller through anyway: this app's LangGraph checkpointer is keyed purely by thread_id
 * with no owner/repo check of its own, so rendering AppShell against a real-but-inaccessible
 * session's thread_id would stream back that session's actual agent state (specs, plans, code)
 * regardless of what this SQL-backed check says -- "proceed" must only ever mean "verified
 * nothing exists yet," never "couldn't confirm, so assume it's fine."
 *
 * A session id is a random UUID now, not a hash only its own owner could ever compute
 * (branch-per-session removed the deterministic (owner, repo, user) formula that used to double
 * as an implicit capability check) -- this is what actually stands between "I have a session id"
 * and "I can see that session's data" now.
 */
export async function lookupSessionWithAuthorization(sessionId: string): Promise<SessionLookupResult> {
  const response = await agentFetch(`sessions/${encodeURIComponent(sessionId)}`);
  if (response.status === 404) return { kind: "not_found" };
  if (!response.ok) return { kind: "denied" }; // fail closed on any other agent-side error too
  const sessionRow = (await response.json()) as Session;
  if (!(await hasRepoAccess(sessionRow.owner, sessionRow.repo))) return { kind: "denied" };
  return { kind: "authorized", session: sessionRow };
}

/**
 * Convenience wrapper for callers that only ever want to render on success and treat every other
 * outcome identically (the `/api/sessions/[sessionId]` route, the report page) -- both
 * "not_found" and "denied" collapse to null, which is correct there since neither caller has a
 * "proceed anyway" path the way the workflow page does.
 */
export async function getAuthorizedSession(sessionId: string): Promise<Session | null> {
  const result = await lookupSessionWithAuthorization(sessionId);
  return result.kind === "authorized" ? result.session : null;
}
