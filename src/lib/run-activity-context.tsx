"use client";

import { createContext, useContext, useState, type ReactNode } from "react";

/** Durable, liveness-relevant slice of the session row AppShell already polls every 10s+focus
 * (`/api/sessions/{threadId}`) -- lifted into a context so child views (BuildView,
 * SessionOverview, SpecificationView, PlanView, RequirementsView) can read `runActive`/
 * `interrupted` without a second fetch. `null` until the first poll resolves -- callers must
 * treat that as "unknown, don't override", never as `runActive: false` (see computeRunningStages'
 * own tri-state handling, use-run-events.ts). */
export type RunActivityInfo = {
  runActive: boolean;
  interrupted: boolean;
  awaitingGate: boolean | null;
  currentStage: string | null;
  status: string;
  /** Root-caused 2026-09-12: status=="failed" alone is ambiguous between a genuine mid-pipeline
   * crash and a run that finished normally with a real report but merge_ready=false -- see
   * agent/src/session_store.py's `is_finished_with_verdict`. True for both "completed" and that
   * finished-but-not-merge-ready shape. */
  finishedWithVerdict: boolean;
  /** Root-caused 2026-09-12: Docker-verified truth (sessions_api._verified_container_alive), not
   * "is a stream attached in THIS process" (that's `interrupted`/`runActive`) -- lets a consumer
   * tell "sandbox alive, cheap free reattach" apart from "sandbox actually gone, needs a real
   * Resume" the same way AppShell's own amber banner already can, instead of asserting a bleaker
   * story than the banner right above it. */
  containerAlive: boolean;
  /** Root-caused 2026-09-12 (pivot): durable failure detail (dbo.sessions.failure_stage/
   * failure_type/failure_message) -- already existed for the session-LIST view
   * (session-types.ts's Session) but never threaded into the workflow page's own context. Lets
   * SessionOverview show which stage actually failed and its real error without needing a live
   * snapshot, and resolve a restart target via workflow-types.ts's `realStageForFailure`. Null
   * whenever the row has no recorded failure (most of the time). */
  failureStage: string | null;
  failureType: string | null;
  failureMessage: string | null;
  /** Root-caused 2026-09-12 (user-reported: run finished merge_ready=false, no in-app way to
   * remedy it cheaply): `finishedWithVerdict` alone collapses "done and ready" and "done but
   * blocked" into one flag -- SessionOverview needs to tell those apart to offer a "Re-verify
   * Metrics & Exit" action only for the latter. `null` until the exit stage has actually run
   * (dbo.sessions.merge_ready is NULL for the entire in_progress lifetime -- see
   * `SessionResponse.merge_ready`, already exposed backend-side, just not threaded here before). */
  mergeReady: boolean | null;
};

const RunActivityContext = createContext<[RunActivityInfo | null, (v: RunActivityInfo | null) => void] | null>(null);

export function RunActivityProvider({ children }: { children: ReactNode }) {
  const state = useState<RunActivityInfo | null>(null);
  return <RunActivityContext.Provider value={state}>{children}</RunActivityContext.Provider>;
}

export function useRunActivity() {
  const ctx = useContext(RunActivityContext);
  if (!ctx) throw new Error("useRunActivity must be used within RunActivityProvider");
  return ctx;
}

/** Same context, but `null` outside a RunActivityProvider instead of throwing -- for views that
 * may render without a live workflow thread. */
export function useOptionalRunActivity() {
  return useContext(RunActivityContext);
}
