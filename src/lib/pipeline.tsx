"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { RebuildPlacement } from "@/lib/workflow-types";

// Mirrors agent/src/pipeline_layout.py's Pipeline.describe() -- served by the agent's GET /pipeline
// (sessions_api.py), fetched server-side by the workflow page and client-side via /api/pipeline.
// Tabs, stage order, labels, gates and mode descriptions all come from here; nothing in src/ should
// hardcode a stage key list again.

export type GatePolicy = "off" | "advisory" | "blocking";

export interface PipelineCheck {
  id: string;
  label: string;
  description: string;
  mode: "blocking" | "collected" | "advisory";
  condition: string;
  needs_audit: boolean;
}

export interface PipelineGate {
  id: string;
  timing: "before_review" | "after_submit";
  persists: boolean;
  /** Keyed by code-gen mode id (PipelineMode.id). */
  policy: Record<string, GatePolicy>;
  checks: PipelineCheck[];
}

export interface PipelineStage {
  key: string;
  label: string;
  description: string;
  gate: PipelineGate | null;
}

export interface PipelineTab {
  id: string;
  /** View-registry key (AppShell's VIEWS); an unknown key renders the generic stage view. */
  view: string;
  label: string;
  /** Stage keys that must all be approved before this tab opens. */
  enable_after: string[];
  /** Top-level WorkflowState keys whose presence opens this tab. */
  enable_state_keys: string[];
  /** "<stage_key>:<status>" transitions that auto-focus this tab. */
  focus_on: string[];
  stages: PipelineStage[];
}

export interface PipelineMode {
  id: string;
  label: string;
  default: boolean;
  /** Whether this mode runs the adversarial audit leg (checks.AUDIT_MODES). */
  audit: boolean;
  blurb: string;
  speed_cost: string;
  best_for: string;
  badge: { text: string; variant: "default" | "secondary" } | null;
}

export interface PipelineDescriptor {
  tabs: PipelineTab[];
  /** The real run sequence (separate from how tabs group stages). */
  order: string[];
  modes: PipelineMode[];
  rebuild_placements: { after_stage_key: string; rebuild_key: string; next_stage_key: string; label: string }[];
  /** Non-stage failure_stage values (rebuild keys, e2e, ...) -> the real stage a restart targets. */
  failure_stage_map: Record<string, string>;
  /** Pre-rename stage keys an old session's stored state may still carry. */
  legacy_labels: Record<string, string>;
  wrapper_checks: PipelineCheck[];
}

/** Pure lookups over one descriptor -- built once per descriptor (usePipeline memoises it). */
export function makePipeline(d: PipelineDescriptor) {
  const stages = new Map(d.tabs.flatMap((t) => t.stages.map((s) => [s.key, s] as const)));
  const tabOf = new Map(d.tabs.flatMap((t) => t.stages.map((s) => [s.key, t] as const)));
  const legacyKeys = Object.keys(d.legacy_labels);
  const rebuildPlacements: RebuildPlacement[] = d.rebuild_placements.map((p) => ({
    afterStageKey: p.after_stage_key,
    rebuildKey: p.rebuild_key,
    nextStageKey: p.next_stage_key,
    label: p.label,
  }));

  /** Index in `order`; legacy keys sort after every real stage (an old session's "exit" still
   * reads as past metrics-exit); -1 for anything unknown. Purely ordinal -- answers "has the
   * durable current_stage moved past X", never implies concurrency. */
  function stageOrderIndex(key: string | null | undefined): number {
    if (!key) return -1;
    const i = d.order.indexOf(key);
    if (i >= 0) return i;
    const legacy = legacyKeys.indexOf(key);
    return legacy >= 0 ? d.order.length + legacy : -1;
  }

  return {
    descriptor: d,
    tabs: d.tabs,
    order: d.order,
    modes: d.modes,
    rebuildPlacements,
    stage: (key: string) => stages.get(key),
    stageLabel: (key: string | null | undefined): string =>
      key == null ? "" : (stages.get(key)?.label ?? d.legacy_labels[key] ?? key),
    stageOrderIndex,
    tabForStage: (key: string | null | undefined) => (key == null ? undefined : tabOf.get(key)),
    nextStageAfter: (key: string | null | undefined): string | undefined => {
      const i = key == null ? -1 : d.order.indexOf(key);
      return i >= 0 ? d.order[i + 1] : undefined;
    },
    /** `dbo.sessions.failure_stage` is not always a real stage key (a rebuild placement, e2e,
     * test_hardening, metrics_report name their own) -- resolves the stage a restart targets.
     * Null for unmapped keys like "provisioning" (nothing was reached to restart). A failed exit
     * with a real verdict is a "View report" case, not a restart -- callers check
     * finished_with_verdict first. */
    realStageForFailure: (failureStage: string | null | undefined): string | null => {
      if (!failureStage) return null;
      if (stageOrderIndex(failureStage) >= 0) return failureStage;
      return d.failure_stage_map[failureStage] ?? null;
    },
    /** This stage's gate policy under `mode`; undefined when the stage has no gate or the mode
     * isn't known yet (render neutral then). */
    gatePolicyFor: (stageKey: string, mode: string | null | undefined): GatePolicy | undefined =>
      mode ? stages.get(stageKey)?.gate?.policy[mode] : undefined,
  };
}

export type Pipeline = ReturnType<typeof makePipeline>;

const PipelineContext = createContext<{ pipeline: Pipeline; sessionCodeGenMode: string | null } | null>(null);

export function PipelineProvider({
  descriptor,
  codeGenMode,
  children,
}: {
  descriptor: PipelineDescriptor;
  /** The session row's code_gen_mode (null before the session exists) -- live state's own
   * code_gen_mode wins over it (useCodeGenMode). */
  codeGenMode: string | null;
  children: ReactNode;
}) {
  const value = useMemo(
    () => ({ pipeline: makePipeline(descriptor), sessionCodeGenMode: codeGenMode }),
    [descriptor, codeGenMode],
  );
  return <PipelineContext.Provider value={value}>{children}</PipelineContext.Provider>;
}

export function usePipeline(): Pipeline {
  const ctx = useContext(PipelineContext);
  if (!ctx) throw new Error("usePipeline must be used inside <PipelineProvider>");
  return ctx.pipeline;
}

/** Prefer the live graph state's mode, fall back to the session row's; null = not known yet. */
export function useCodeGenMode(stateMode: string | null | undefined): string | null {
  const sessionMode = useContext(PipelineContext)?.sessionCodeGenMode;
  return stateMode ?? sessionMode ?? null;
}

// Client-only pages with no server parent to fetch for them (board, session lists): one shared
// fetch of /api/pipeline per page load. Cleared on failure so a later mount retries.
let fetched: Promise<PipelineDescriptor | null> | null = null;

export function useFetchedPipeline(): Pipeline | null {
  const [descriptor, setDescriptor] = useState<PipelineDescriptor | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetched ??= fetch("/api/pipeline")
      .then((res) => (res.ok ? (res.json() as Promise<PipelineDescriptor>) : null))
      .catch(() => null);
    void fetched.then((d) => {
      if (d == null) fetched = null;
      if (!cancelled) setDescriptor(d);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return useMemo(() => (descriptor ? makePipeline(descriptor) : null), [descriptor]);
}
