import { createHash } from "crypto";

/**
 * Deterministic LangGraph thread id for a (repo, user) combination (architecture plan Section A;
 * revised for the single-ai-dev-workflow-branch-per-repo migration -- branch is no longer part of
 * a session's identity, only a provision-time PR-target parameter, so it drops out of this key).
 * Keyed by user, not just repo: two different teammates opening the same shared/org repo land on
 * distinct LangGraph threads, so they don't share chat history.
 *
 * Honest trade-off, not isolation: with one work branch shared by every session on a repo, every
 * user's thread ultimately reads and writes the SAME `.ai-dev-workflow/state.json` on that branch
 * -- user B's thread hydrates whatever user A most recently approved (draft specs/plans
 * included). Accepted for a repo-scoped internal tool; do not rely on thread separation for
 * per-user isolation of drafts or approvals.
 *
 * Same repo/user always resolves to the same thread, from any browser or machine, since it's a
 * pure function of these inputs.
 */
export function deriveThreadId(owner: string, repo: string, githubUserId: string): string {
  const key = `${owner}/${repo}:${githubUserId}`;
  return createHash("sha256").update(key).digest("hex");
}

/** The local (per-hook) CopilotKit agentId used to scope a proxied agent to one thread --
 * useAgent's threadId requires a distinct runtimeAgentId + local agentId pairing (see
 * workflow-thread-context.tsx), never the shared "workflow" registry id directly. */
export function deriveLocalAgentId(threadId: string): string {
  return `workflow-thread-${threadId}`;
}
