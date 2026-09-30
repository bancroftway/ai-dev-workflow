"use client";

import { GateButton } from "@/components/GateButton";
import type { PipelineTab } from "@/lib/pipeline";

/** The gate icon between stage tabs: AppShell renders one right after every tab, and this decides
 * whether that tab has anything to gate. `codeGenMode` null = mode not known yet (gate neutral). */
export function GateSlot({
  tab,
  codeGenMode,
  owner,
  repo,
}: {
  tab: PipelineTab;
  codeGenMode: string | null;
  owner: string;
  repo: string;
}) {
  const gatedStages = tab.stages.filter((s) => s.gate != null);
  if (gatedStages.length === 0) return null;
  return <GateButton stages={gatedStages} codeGenMode={codeGenMode} label={tab.label} owner={owner} repo={repo} />;
}
