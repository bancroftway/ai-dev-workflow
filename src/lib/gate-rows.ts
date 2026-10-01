// Pure row-state derivation for the gate dialog (GateButton.tsx, plan §6). No React, no runtime
// imports -- scripts/check-gate-rows.mjs runs this file directly under Node's type stripping.
import type { GatePolicy, PipelineCheck } from "@/lib/pipeline";

/** One reported check row, as the agent's CheckLog records it (agent/src/gates/checks.py). */
export interface ReportedCheck {
  id: string;
  status: "passed" | "failed" | "infra" | "skipped" | "advisory";
  detail: string | null;
  source: string;
  uncatalogued?: boolean;
}

/** The verdict a table is drawn from: the live `last_verification`, or one history attempt. */
export interface GateVerdict {
  passed: boolean;
  cannot_verify?: boolean;
  /** Absent = a verdict recorded before per-check rows existed. */
  checks?: ReportedCheck[];
}

export type RowTone = "pass" | "fail" | "warn" | "muted";

export interface GateRow {
  id: string;
  label: string;
  description: string;
  mode: string;
  condition: string;
  group: "stage" | "wrapper" | "uncatalogued";
  /** Machine-readable state (the self-check asserts on it). */
  state: string;
  /** What the Status column shows. */
  text: string;
  tone: RowTone;
  detail: string | null;
  source: string | null;
  uncatalogued: boolean;
  /** "lap N (redraft in progress)" while the stage redrafts after a failed lap. */
  lapNote: string | null;
}

export interface GateRowInput {
  checks: PipelineCheck[];
  wrapperChecks: PipelineCheck[];
  verdict: GateVerdict | null;
  /** undefined = mode not known yet. */
  policy: GatePolicy | undefined;
  modeLabel: string | null;
  /** undefined = mode not known yet (audit runs in mission_critical only). */
  auditOn: boolean | undefined;
  /** Live stage status; null when drawing a history attempt. */
  stageStatus: string | null;
  lap: number;
}

const REPORTED: Record<ReportedCheck["status"], { text: string; tone: RowTone }> = {
  passed: { text: "Passed", tone: "pass" },
  failed: { text: "Failed", tone: "fail" },
  infra: { text: "Couldn't run (platform issue)", tone: "warn" },
  skipped: { text: "Skipped", tone: "muted" },
  advisory: { text: "Heads-up (doesn't block)", tone: "warn" },
};

function isBlockingFailure(r: ReportedCheck, catalog: Map<string, PipelineCheck>): boolean {
  return (r.status === "failed" || r.status === "infra") && catalog.get(r.id)?.mode === "blocking";
}

export function deriveGateRows(input: GateRowInput): GateRow[] {
  const { verdict, policy, modeLabel, auditOn, stageStatus, lap } = input;
  const reported = new Map((verdict?.checks ?? []).map((r) => [r.id, r] as const));
  const catalog = new Map([...input.wrapperChecks, ...input.checks].map((c) => [c.id, c] as const));
  const redraft = verdict != null && !verdict.passed && stageStatus === "drafting";
  const lapNote = redraft ? `lap ${lap} (redraft in progress)` : null;

  // A reported blocking failure stops the chain: a wrapper failure stops every stage row; a stage
  // failure stops the stage rows after it (catalog order).
  const reportedBlocking = (verdict?.checks ?? []).filter((r) => isBlockingFailure(r, catalog));
  const wrapperStopped = reportedBlocking.some((r) => input.wrapperChecks.some((w) => w.id === r.id));
  const firstStageStop = Math.min(
    ...reportedBlocking.map((r) => input.checks.findIndex((c) => c.id === r.id)).filter((i) => i >= 0),
  );

  function unreported(c: PipelineCheck, index: number, group: GateRow["group"]): Pick<GateRow, "state" | "text" | "tone"> {
    if (verdict) {
      if (verdict.cannot_verify) return { state: "no_sandbox", text: "Not run: no sandbox", tone: "warn" };
      if (!("checks" in verdict) || verdict.checks == null)
        return {
          state: "no_detail",
          text: `${verdict.passed ? "Passed" : "Failed"}, no per-check detail recorded`,
          tone: "muted",
        };
      if (c.needs_audit && auditOn === false) return { state: "audit_off", text: "Skipped: no audit", tone: "muted" };
      const reached = group === "stage" ? !wrapperStopped && !(index > firstStageStop) : true;
      if (!reached) return { state: "not_reached", text: "Not reached (an earlier check stopped it)", tone: "muted" };
      return { state: "not_recorded", text: "Not recorded", tone: "muted" };
    }
    if (policy === "off")
      return { state: "policy_off", text: `Skipped: not enforced in ${modeLabel ?? "this mode"}`, tone: "muted" };
    if (stageStatus === "approved")
      return { state: "approved_earlier", text: "Approved earlier, not re-verified this run", tone: "muted" };
    if (c.needs_audit && auditOn === false) return { state: "audit_off", text: "Skipped: no audit", tone: "muted" };
    return { state: "will_run", text: "Will run", tone: "muted" };
  }

  function row(c: PipelineCheck, index: number, group: GateRow["group"]): GateRow {
    const r = reported.get(c.id);
    const base = { id: c.id, label: c.label, description: c.description, mode: c.mode, condition: c.condition, group, lapNote };
    if (!r) return { ...base, ...unreported(c, index, group), detail: null, source: null, uncatalogued: false };
    // An advisory-mode check's failure doesn't block: amber, not red.
    const shown = r.status === "failed" && c.mode === "advisory" ? { text: "Heads-up (doesn't block)", tone: "warn" as const } : REPORTED[r.status];
    return { ...base, state: r.status, ...shown, detail: r.detail, source: r.source, uncatalogued: !!r.uncatalogued };
  }

  const rows = [
    ...input.checks.map((c, i) => row(c, i, "stage")),
    ...input.wrapperChecks.map((c, i) => row(c, i, "wrapper")),
  ];
  // Reported ids the descriptor doesn't know (a check added agent-side without a catalog entry).
  for (const r of verdict?.checks ?? []) {
    if (catalog.has(r.id)) continue;
    rows.push({
      id: r.id, label: r.id, description: "", mode: "", condition: "", group: "uncatalogued", lapNote,
      state: r.status, ...REPORTED[r.status], detail: r.detail, source: r.source, uncatalogued: true,
    });
  }
  return rows;
}
