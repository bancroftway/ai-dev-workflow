/**
 * The one TypeScript shape for a session row -- mirrors agent/src/sessions_api.py's
 * `SessionResponse` exactly. Every route/component that touches a session imports this instead of
 * re-declaring its own interface (the old `.ai-dev-workflow/sessions.json`-era code had two
 * near-duplicate `SessionEntry` types; don't repeat that).
 */
export type Session = {
  session_id: string;
  owner: string;
  repo: string;
  user_login: string;
  title: string;
  source_branch: string;
  work_branch: string;
  run_id: string | null;
  current_stage: string | null;
  status: "in_progress" | "completed" | "failed" | "rejected";
  started_at: string;
  ended_at: string | null;
  merge_ready: boolean | null;
  pr_title: string | null;
  pr_url: string | null;
  failure_stage: string | null;
  failure_type: string | null;
  failure_message: string | null;
  /** Live, not persisted -- whether this session's sandbox is currently registered in the
   * agent's memory right now. False after an agent restart until the session is reprovisioned,
   * regardless of `status`. */
  container_alive: boolean;
};

/** agent/src/graph.py's STAGES list, key order -- used only to render "stage N of M" in the
 * session-list progress indicator. app-discovery/brownfield-baseline run between tech-stack and
 * specification but aren't StageSpec entries themselves (separate wired sub-flows), so
 * current_stage never reports them -- this list intentionally matches STAGES exactly, not the
 * full graph. Cosmetic only: if the two ever drift, the indicator just falls back to showing the
 * raw stage key (see SessionHistory.tsx), never breaks. */
export const STAGE_KEYS_IN_ORDER = [
  "tech-stack",
  "specification",
  "plan",
  "ac-to-tests",
  "minimal-code-to-green",
  "remediation",
  "adversarial-compliance",
  "metrics-exit",
] as const;
