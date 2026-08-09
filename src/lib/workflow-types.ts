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
}

export interface WorkflowState {
  raw_requirements_text?: string;
  stages?: {
    specification?: StageState;
    plan?: StageState;
  };
}

export interface GatePayload {
  stage: "specification" | "plan";
  draft: unknown;
}
