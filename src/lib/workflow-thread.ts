import { createHash } from "crypto";

/**
 * Deterministic LangGraph thread id for a (repo, branch, user) combination (architecture plan
 * Section A). Keyed by user, not just repo/branch: two different teammates opening the same
 * shared/org repo must land on distinct threads, not share chat history, drafts, and gate
 * approvals -- see the plan's "Design gap to close" note. Same repo/branch/user always resolves
 * to the same thread, from any browser or machine, since it's a pure function of these inputs.
 */
export function deriveThreadId(owner: string, repo: string, branch: string, githubUserId: string): string {
  const key = `${owner}/${repo}@${branch}:${githubUserId}`;
  return createHash("sha256").update(key).digest("hex");
}

/** The local (per-hook) CopilotKit agentId used to scope a proxied agent to one thread --
 * useAgent's threadId requires a distinct runtimeAgentId + local agentId pairing (see
 * workflow-thread-context.tsx), never the shared "workflow" registry id directly. */
export function deriveLocalAgentId(threadId: string): string {
  return `workflow-thread-${threadId}`;
}
