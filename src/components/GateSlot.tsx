"use client";

import { GateButton, GateView } from "@/components/GateButton";
import type { PipelineTab } from "@/lib/pipeline";

/** View id of a tab's gate in AppShell's tab strip ("gate:<tab id>"). */
export const gateViewId = (tab: PipelineTab) => `gate:${tab.id}`;

const gatedStages = (tab: PipelineTab) => tab.stages.filter((s) => s.gate != null);

/** The gate tab AppShell renders right after every stage tab; null when the tab gates nothing.
 * `codeGenMode` null = mode not known yet (gate neutral). */
export function GateSlot({
  tab,
  codeGenMode,
  active,
  onSelect,
}: {
  tab: PipelineTab;
  codeGenMode: string | null;
  active: boolean;
  onSelect: () => void;
}) {
  const stages = gatedStages(tab);
  if (stages.length === 0) return null;
  return <GateButton stages={stages} codeGenMode={codeGenMode} label={tab.label} active={active} onSelect={onSelect} />;
}

/** The selected gate tab's screen, or null when the tab gates nothing. */
export function GatePanel({
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
  const stages = gatedStages(tab);
  if (stages.length === 0) return null;
  return <GateView stages={stages} codeGenMode={codeGenMode} label={tab.label} owner={owner} repo={repo} />;
}
