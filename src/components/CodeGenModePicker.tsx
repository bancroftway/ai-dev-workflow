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

/** "yolo" | "draft_verify" | "mission_critical" -- matches agent/src/graph.py's
 * GraphState.code_gen_mode wire values exactly (Task 1, plan Part 2). Sent as-is in the provision
 * POST body (SandboxSessionBoot.tsx) as `codeGenMode`, which route.ts forwards as `code_gen_mode`. */
export type CodeGenMode = "yolo" | "draft_verify" | "mission_critical";

const DEFAULT_MODE: CodeGenMode = "draft_verify";

type ModeInfo = {
  mode: CodeGenMode;
  name: string;
  /** What actually runs -- draft only / draft+check / draft+audit+check. */
  explanation: string;
  /** Speed + token/time cost, condensed from the plan's mode table. */
  speedCost: string;
  bestFor: string;
  badge?: { text: string; variant: "default" | "secondary" };
};

const MODES: ModeInfo[] = [
  {
    mode: "yolo",
    name: "⚡ YOLO",
    explanation: "Draft only — no second-opinion audit, no deterministic check before advancing.",
    speedCost:
      "Instant · 1 LLM call per stage (draft only, pipeline-wide) — the cheapest and fastest option.",
    bestFor:
      "Best for quick prototypes, throwaway spikes, scratch scripts — anything you'll read and test yourself end-to-end before it matters.",
    badge: { text: "Some checks still run in the background (not enforced)", variant: "secondary" },
  },
  {
    mode: "draft_verify",
    name: "🪵 Draft & Verify",
    explanation:
      "Draft, plus each stage's own free, deterministic check (ledger sync, diagram validity, test coverage, remediation confirmation, compliance audit, exit readiness) — no second-opinion audit.",
    speedCost:
      "Fast · 1 LLM call per stage, plus each stage's own free check. Only redrafts — costing another LLM call — if a check actually fails.",
    bestFor:
      "Best for day-to-day work end to end — every stage's own deterministic check still runs, without paying for a second model to review every draft regardless of whether it's needed.",
    badge: { text: "Recommended", variant: "default" },
  },
  {
    mode: "mission_critical",
    name: "🛡️ Mission Critical",
    explanation:
      "Draft, a second-opinion audit, and the same deterministic check/redraft loop as Draft & Verify — at every stage.",
    speedCost:
      "Thorough · 2+ LLM calls per audited stage (draft + audit) — meaningfully more tokens and wall-clock time across the whole run.",
    bestFor:
      "Best for production-critical work — auth, payments, security-sensitive code, complex refactors of core systems, anything you won't hand-review line by line yourself, end to end.",
  },
];

/**
 * One-time "how carefully should this session work" choice (plan Part 2 Task 3). Shown by
 * SandboxSessionBoot.tsx before it ever fires the provision fetch for a genuinely brand-new
 * session -- pure presentation here, the caller owns persisting the choice (sessionStorage) and
 * threading it into the provision POST body. No cancel affordance: `open` is pinned true and the
 * only way out is the confirm button, since there is no meaningful "resume without a mode" state
 * for a session that doesn't exist yet.
 */
export function CodeGenModePicker({ onSelect }: { onSelect: (mode: CodeGenMode) => void }) {
  const [selected, setSelected] = useState<CodeGenMode>(DEFAULT_MODE);

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
          defaultValue={DEFAULT_MODE}
          onValueChange={(value) => setSelected(value as CodeGenMode)}
          className="gap-3"
        >
          {MODES.map((info) => (
            <Label key={info.mode} className="block cursor-pointer font-normal">
              <Card className="flex-row items-start gap-3 p-4">
                <RadioGroupItem value={info.mode} className="mt-1" />
                <CardContent className="flex-1 p-0">
                  <CardTitle className="flex flex-wrap items-center gap-2 text-sm">
                    {info.name}
                    {info.badge && <Badge variant={info.badge.variant}>{info.badge.text}</Badge>}
                  </CardTitle>
                  <p className="mt-1 text-xs text-muted-foreground">{info.explanation}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{info.speedCost}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{info.bestFor}</p>
                </CardContent>
              </Card>
            </Label>
          ))}
        </RadioGroup>
        <p className="text-xs text-muted-foreground">
          YOLO skips the second opinion and every stage&apos;s own deterministic check; the same
          underlying checks still run as one-time, same-turn nudges via baked-in scans across
          every stage — real protection, but not the guaranteed, repeatable check Mission Critical
          provides.
        </p>
        <Button onClick={() => onSelect(selected)} className="w-full">
          Start session
        </Button>
      </DialogContent>
    </Dialog>
  );
}
