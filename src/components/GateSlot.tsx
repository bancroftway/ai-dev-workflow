"use client";

import type { PipelineTab } from "@/lib/pipeline";

/** Seam for the gate icon between stage tabs (plan §6's <GateButton>): AppShell renders one right
 * after every tab, and this decides whether that tab has anything to gate. Renders nothing yet --
 * the dialog lands separately. `codeGenMode` null = mode not known yet (render the gate neutral). */
export function GateSlot({ tab, codeGenMode }: { tab: PipelineTab; codeGenMode: string | null }) {
  const gatedStages = tab.stages.filter((s) => s.gate != null);
  if (gatedStages.length === 0) return null;
  void codeGenMode; // -> <GateButton stages={gatedStages} codeGenMode={codeGenMode} />
  return null;
}
