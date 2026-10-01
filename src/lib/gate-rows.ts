// Pure row-state derivation for the gate dialog (GateButton.tsx, plan §6). No React, no runtime
// imports -- scripts/check-gate-rows.mjs runs this file directly under Node's type stripping.
// All user-visible copy comes from the backend descriptor (pipeline_layout.GATE_TEXT).
import type { GatePolicy, GateText, PipelineCheck } from "@/lib/pipeline";

/** Fills `{name}` placeholders in a descriptor gate_text template. */
export function fmt(template: string, vars: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}

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
  /** The descriptor's gate_text.row_status: the Status column's copy. */
  text: GateText["row_status"];
}

const REPORTED_TONE: Record<ReportedCheck["status"], RowTone> = {
  passed: "pass",
  failed: "fail",
  infra: "warn",
  skipped: "muted",
  advisory: "warn",
};

function isBlockingFailure(r: ReportedCheck, catalog: Map<string, PipelineCheck>): boolean {
  return (r.status === "failed" || r.status === "infra") && catalog.get(r.id)?.mode === "blocking";
}

export function deriveGateRows(input: GateRowInput): GateRow[] {
  const { verdict, policy, modeLabel, auditOn, stageStatus, lap, text: T } = input;
  const reportedShown = (s: ReportedCheck["status"]) => ({ text: T[s], tone: REPORTED_TONE[s] });
  const reported = new Map((verdict?.checks ?? []).map((r) => [r.id, r] as const));
  const catalog = new Map([...input.wrapperChecks, ...input.checks].map((c) => [c.id, c] as const));
  const redraft = verdict != null && !verdict.passed && stageStatus === "drafting";
  const lapNote = redraft ? fmt(T.lap_note, { lap }) : null;

  // A reported blocking failure stops the chain: a wrapper failure stops every stage row; a stage
  // failure stops the stage rows after it (catalog order).
  const reportedBlocking = (verdict?.checks ?? []).filter((r) => isBlockingFailure(r, catalog));
  const wrapperStopped = reportedBlocking.some((r) => input.wrapperChecks.some((w) => w.id === r.id));
  const firstStageStop = Math.min(
    ...reportedBlocking.map((r) => input.checks.findIndex((c) => c.id === r.id)).filter((i) => i >= 0),
  );

  function unreported(c: PipelineCheck, index: number, group: GateRow["group"]): Pick<GateRow, "state" | "text" | "tone"> {
    if (verdict) {
      if (verdict.cannot_verify) return { state: "no_sandbox", text: T.no_sandbox, tone: "warn" };
      if (!("checks" in verdict) || verdict.checks == null)
        return { state: "no_detail", text: verdict.passed ? T.no_detail_passed : T.no_detail_failed, tone: "muted" };
      if (c.needs_audit && auditOn === false) return { state: "audit_off", text: T.audit_off, tone: "muted" };
      const reached = group === "stage" ? !wrapperStopped && !(index > firstStageStop) : true;
      if (!reached) return { state: "not_reached", text: T.not_reached, tone: "muted" };
      return { state: "not_recorded", text: T.not_recorded, tone: "muted" };
    }
    if (policy === "off")
      return { state: "policy_off", text: fmt(T.policy_off, { mode: modeLabel ?? T.policy_off_mode_fallback }), tone: "muted" };
    if (stageStatus === "approved") return { state: "approved_earlier", text: T.approved_earlier, tone: "muted" };
    if (c.needs_audit && auditOn === false) return { state: "audit_off", text: T.audit_off, tone: "muted" };
    return { state: "will_run", text: T.will_run, tone: "muted" };
  }

  function row(c: PipelineCheck, index: number, group: GateRow["group"]): GateRow {
    const r = reported.get(c.id);
    const base = { id: c.id, label: c.label, description: c.description, mode: c.mode, condition: c.condition, group, lapNote };
    if (!r) return { ...base, ...unreported(c, index, group), detail: null, source: null, uncatalogued: false };
    // An advisory-mode check's failure doesn't block: amber, not red.
    const shown = r.status === "failed" && c.mode === "advisory" ? { text: T.advisory_failed, tone: "warn" as const } : reportedShown(r.status);
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
      state: r.status, ...reportedShown(r.status), detail: r.detail, source: r.source, uncatalogued: true,
    });
  }
  return rows;
}
