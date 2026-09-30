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
  /** True while `current_stage` is paused at its own human gate awaiting approval (Part 3 Task 1
   * `dbo.sessions.awaiting_gate`, threaded through `SessionResponse` for Task 9's board). Null/false
   * the rest of the time, including for every terminal (completed/failed/rejected) row. */
  awaiting_gate: boolean | null;
  started_at: string;
  ended_at: string | null;
  merge_ready: boolean | null;
  pr_title: string | null;
  pr_url: string | null;
  failure_stage: string | null;
  failure_type: string | null;
  failure_message: string | null;
  /** "yolo" | "draft_verify" | "mission_critical" -- the code generation mode this session's gates
   * run under (agent/src/sessions_api.py resolves an unset stored value to "mission_critical",
   * the same fallback graph.py applies). Optional so an older agent without the field still types. */
  code_gen_mode?: string | null;
  /** Live, not persisted -- whether this session's sandbox is currently registered in the
   * agent's memory right now. False after an agent restart until the session is reprovisioned,
   * regardless of `status`. */
  container_alive: boolean;
  /** Live, not persisted -- this session's process-local run_activity refcount is > 0 right now,
   * i.e. an AG-UI stream is actually attached and executing (Workflow Liveness Fix). Unlike
   * `status`, this does not survive a crash/restart -- which is the point. */
  run_active: boolean;
  /** Derived server-side (not a DB status): `status === "in_progress"` but neither `run_active`
   * nor `awaiting_gate` -- the workflow isn't finished, but nothing is currently executing it. */
  interrupted: boolean;
  /** Derived server-side (root-caused 2026-09-12): `status === "completed"`, or `status ===
   * "failed"` with `failure_stage === "exit"` -- a run that reached the real end of the pipeline
   * with a real report, whether merge_ready came back true or false. `status === "failed"` alone
   * is ambiguous with a genuine mid-pipeline crash; this field isn't. See
   * agent/src/session_store.py's `is_finished_with_verdict` for the authoritative definition. */
  finished_with_verdict: boolean;
};
