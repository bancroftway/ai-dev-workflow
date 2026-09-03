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
