"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

/** What the currently-open interrupt (approval gate or escalation) carries.
 *
 * Two consumers beyond the banner itself:
 * - RequirementsView disables Submit while an interrupt is open — a run started during a pause
 *   is silently dropped server-side (ag_ui_langgraph re-emits the stored interrupt and never
 *   starts the graph), so an enabled Submit is a lie.
 * - Specification/Plan views use `draft` as a last-resort render source after a reload with an
 *   open gate: the re-emitted interrupt payload is the ONLY data the transport delivers then
 *   (no state or message snapshots are streamed in that path).
 */
export interface InterruptInfo {
  open: boolean;
  stage?: string;
  draft?: unknown;
}

const InterruptContext = createContext<{
  interrupt: InterruptInfo;
  setInterrupt: (info: InterruptInfo) => void;
}>({ interrupt: { open: false }, setInterrupt: () => {} });

export function InterruptProvider({ children }: { children: ReactNode }) {
  const [interrupt, setInterrupt] = useState<InterruptInfo>({ open: false });
  const value = useMemo(() => ({ interrupt, setInterrupt }), [interrupt]);
  return <InterruptContext.Provider value={value}>{children}</InterruptContext.Provider>;
}

export function useOpenInterrupt() {
  return useContext(InterruptContext);
}
