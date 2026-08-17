/**
 * LangGraph thread id == the session id (a UUID minted client-side for a new session, or an
 * existing session's own id on resume -- src/app/select/page.tsx, src/app/api/sessions/provision).
 * There is no deterministic derivation anymore: branch-per-session means each session has its own
 * git branch, so (owner, repo, user) can no longer resolve to a single shared thread the way it
 * did under the one-work-branch-per-repo design this replaced.
 */

/** The local (per-hook) CopilotKit agentId used to scope a proxied agent to one thread --
 * useAgent's threadId requires a distinct runtimeAgentId + local agentId pairing (see
 * workflow-thread-context.tsx), never the shared "workflow" registry id directly. */
export function deriveLocalAgentId(threadId: string): string {
  return `workflow-thread-${threadId}`;
}
