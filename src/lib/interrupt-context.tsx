"use client";

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

/** What the currently-open interrupt (approval gate or escalation) carries.
 *
 * Consumers beyond the banner itself:
 * - RequirementsView disables Submit while an interrupt is open — a run started during a pause
 *   is silently dropped server-side (ag_ui_langgraph re-emits the stored interrupt and never
 *   starts the graph), so an enabled Submit is a lie.
 * - Specification/Plan views use `draft` as a last-resort render source after a reload with an
 *   open gate: the re-emitted interrupt payload is the ONLY data the transport delivers then
 *   (no state or message snapshots are streamed in that path).
 * - TechStackView is the first consumer of `resolve` directly (every other gate resolves via the
 *   generic InterruptCard "Approve" button instead) -- it needs to hand back the human's EDITED
 *   markdown, not just approve the draft verbatim. `draftMarkdown`/`fileExisted` are tech-stack-
 *   specific (graph.py's build_interrupt_extra for that stage); undefined for every other gate.
 */
export interface InterruptInfo {
  open: boolean;
  stage?: string;
  draft?: unknown;
  draftMarkdown?: string;
  fileExisted?: boolean;
  resolve?: (value: unknown) => void;
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
