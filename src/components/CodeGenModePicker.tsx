"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { usePipeline, type GatePolicy, type Pipeline } from "@/lib/pipeline";

/** A PipelineMode.id from the backend descriptor ("yolo" | "draft_verify" | "mission_critical"
 * today) -- matches agent/src/graph.py's GraphState.code_gen_mode wire values. Sent as-is in the
 * provision POST body (SandboxSessionBoot.tsx) as `codeGenMode`, which route.ts forwards as
 * `code_gen_mode`. */
export type CodeGenMode = string;

/** "What's checked" under `mode`, from each stage gate's per-mode policy -- the backend's
 * policy table, not copy that can drift from it. */
function checkedSummary(pipeline: Pipeline, mode: string): string {
  const by: Record<GatePolicy, string[]> = { blocking: [], advisory: [], off: [] };
  for (const tab of pipeline.tabs) {
    for (const stage of tab.stages) {
      const policy = stage.gate?.policy[mode];
      if (policy) by[policy].push(stage.label);
    }
  }
  return [
    by.blocking.length > 0 && `Enforced: ${by.blocking.join(", ")}`,
    by.advisory.length > 0 && `Advisory: ${by.advisory.join(", ")}`,
    by.off.length > 0 && `Not checked: ${by.off.join(", ")}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * One-time "how carefully should this session work" choice (plan Part 2 Task 3). Shown by
 * SandboxSessionBoot.tsx before it ever fires the provision fetch for a genuinely brand-new
 * session -- pure presentation here, the caller owns persisting the choice (sessionStorage) and
 * threading it into the provision POST body. No cancel affordance: `open` is pinned true and the
 * only way out is the confirm button, since there is no meaningful "resume without a mode" state
 * for a session that doesn't exist yet.
 */
export function CodeGenModePicker({ onSelect }: { onSelect: (mode: CodeGenMode) => void }) {
  const pipeline = usePipeline();
  const defaultMode = (pipeline.modes.find((m) => m.default) ?? pipeline.modes[0])?.id ?? "";
  const [selected, setSelected] = useState<CodeGenMode>(defaultMode);

  return (
    <Dialog open onOpenChange={() => {}}>
      <DialogContent showCloseButton={false} className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Choose a code generation mode</DialogTitle>
          <DialogDescription>
            This sets how carefully the agent works for this session. It can&apos;t be changed
            after the session starts.
          </DialogDescription>
        </DialogHeader>
        <RadioGroup
          defaultValue={defaultMode}
          onValueChange={(value) => setSelected(value as CodeGenMode)}
          className="gap-3"
        >
          {pipeline.modes.map((info) => (
            <Label key={info.id} className="block cursor-pointer font-normal">
              <Card className="flex-row items-start gap-3 p-4">
                <RadioGroupItem value={info.id} className="mt-1" />
                <CardContent className="flex-1 p-0">
                  <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
                    {info.label}
                    {info.badge && <Badge variant={info.badge.variant}>{info.badge.text}</Badge>}
                  </CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">{info.blurb}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{info.speed_cost}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{info.best_for}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{checkedSummary(pipeline, info.id)}</p>
                </CardContent>
              </Card>
            </Label>
          ))}
        </RadioGroup>
        <Button onClick={() => onSelect(selected)} className="w-full">
          Start session
        </Button>
      </DialogContent>
    </Dialog>
  );
}
