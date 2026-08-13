// Mirrors agent/src/graph.py's GraphState/StageState shape (SPECIFICATION.md Section 4/5).

export type StageStatus =
  | "not_started"
  | "drafting"
  | "needs_clarification"
  | "ready_for_review"
  | "approved";

export interface ClarifyingQuestion {
  id: string;
  question: string;
  suggested_choices: string[];
}

export interface StageState {
  status: StageStatus;
  draft: unknown;
  clarifying_questions: ClarifyingQuestion[];
  readiness: boolean;
  cycle_count: number;
  approved_content: unknown;
  ever_ready_for_review: boolean;
  used_ids: string[];
  audit_findings: string[];
}

export interface WorkflowState {
  raw_requirements_text?: string;
  stages?: {
    "p0-brownfield"?: StageState;
    "tech-stack"?: StageState;
    "raw-requirements"?: StageState;
    specification?: StageState;
    plan?: StageState;
    "ac-to-tests"?: StageState;
    "minimal-code-to-green"?: StageState;
    "p11a-adversarial-audit"?: StageState;
    "p11b-dedup"?: StageState;
    "p11d-license-audit"?: StageState;
    "p15-exit"?: StageState;
  };
}

// Ordered pipeline sequence -- drives AppShell's gate label lookup (the first stage in this
// order currently "ready_for_review" is the one paused on the open interrupt) and the Session
// Overview panel's timeline. Extend this list, not a hardcoded ternary, as more gated stages land.
// Bespoke node clusters (P8/P10/P11c/P13/P14) have no StageState/gate of this shape and are
// intentionally absent here -- the Session Overview panel reads state.stages dynamically, so their
// absence from this static list doesn't hide them from that panel, only from this ordered lookup.
export const PIPELINE_STAGE_ORDER: { key: keyof NonNullable<WorkflowState["stages"]>; label: string }[] = [
  { key: "p0-brownfield", label: "Preflight Baseline" },
  { key: "tech-stack", label: "Tech Stack" },
  { key: "raw-requirements", label: "Raw Requirements" },
  { key: "specification", label: "Specification" },
  { key: "plan", label: "Implementation Plan" },
  { key: "ac-to-tests", label: "Acceptance Criteria to Tests" },
  { key: "minimal-code-to-green", label: "Minimal Code to Green" },
  { key: "p11a-adversarial-audit", label: "Adversarial Audit" },
  { key: "p11b-dedup", label: "De-dup / Simplify" },
  { key: "p11d-license-audit", label: "License Audit" },
  { key: "p15-exit", label: "Exit" },
];

export interface GatePayload {
  stage:
    | "p0-brownfield"
    | "tech-stack"
    | "raw-requirements"
    | "specification"
    | "plan"
    | "ac-to-tests"
    | "minimal-code-to-green"
    | "p11a-adversarial-audit"
    | "p11b-dedup"
    | "p11d-license-audit"
    | "p15-exit";
  draft: unknown;
}
