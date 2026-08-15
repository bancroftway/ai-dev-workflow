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

/** Mirrors graph.py's VerificationResult, as stored on StageState.last_verification. */
export interface StageVerification {
  passed: boolean;
  feedback: string;
  report: unknown;
  cannot_verify?: boolean;
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
  verify_cycle_count: number;
  last_verification: StageVerification | null;
  baseline_commit: string | null;
}

/** Set by agent/src/app_discovery.py's decide node when the repository contains no startable
 * application. The one hard stop in the pipeline -- the run ends rather than pausing, so this is
 * the only signal the human gets, alongside the chat message the reject node posts. */
export interface AppRejection {
  reasons: string[];
  found: { path: string; app_class: string }[];
  checked_at: string;
}

/** repo_scan.py's ScanReport.summary() shape -- streamed via the repo_scan state channel. */
export interface ScanSummary {
  health_score: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
  deduped_count: number;
  gating_count: number;
  severity_floor: string;
}

/** Curated scan snapshot streamed on GraphState.repo_scan for the metrics bar -- small keys only,
 * full findings stay in the committed .ai-dev-workflow/repo-scan-*.json files. */
export interface RepoScanState {
  baseline?: string | null;
  baseline_summary?: ScanSummary | null;
  latest_summary?: ScanSummary | null;
  latest_duplication_percent?: number | null;
  coverage?: { line_rate: number | null; branch_rate: number | null };
  delta_summary?: unknown;
  reason?: string;
}

/** Finding rows as quality/security remediation stream them (repo_scan _dashboard_finding shape,
 * plus triage decoration). Loosely typed on purpose -- QualityView renders what's present. */
export interface RemediationFinding {
  finding_key?: string;
  id?: string;
  severity?: string;
  category?: string;
  rule?: string;
  message?: string;
  file?: string;
  line?: number | null;
  [key: string]: unknown;
}

export interface QualityRemediationState {
  cycle_count: number;
  findings: RemediationFinding[];
  decisions: Record<string, { decision: string; justification: string; ref?: string }>;
  duplication_percent: number | null;
  format_clean: boolean | null;
  build_ok: boolean;
  last_gate_report: { passed?: boolean; [key: string]: unknown } | null;
}

export interface SecurityRemediationState {
  cycle_count: number;
  findings: RemediationFinding[];
  decisions: Record<string, { decision: string; justification: string; ref?: string }>;
  sbom_ok: boolean;
  last_gate_report: { passed?: boolean; [key: string]: unknown } | null;
}

export interface TestHardeningState {
  stable_fail?: string[];
  flaky?: string[];
  last_exit_ok?: boolean;
  [key: string]: unknown;
}

export interface MetricsReportState {
  metrics?: {
    coverage?: { line_rate: number | null; branch_rate: number | null };
    traceability_summary?: { total: number; covered: number; tests_only: number; untested: number };
    [key: string]: unknown;
  };
}

/** Outcome of the last push to the ai-dev-workflow/<branch> work branch (git_ops.push_head).
 * ok=false means GitHub persistence is currently failing (e.g. no push permission) -- local
 * commits continue regardless. */
export interface PushStatus {
  ok: boolean;
  error?: string | null;
  at?: string;
}

export interface WorkflowState {
  raw_requirements_text?: string;
  app_rejection?: AppRejection | null;
  repo_scan?: RepoScanState;
  quality_remediation?: QualityRemediationState;
  security_remediation?: SecurityRemediationState;
  test_hardening?: TestHardeningState;
  metrics_report?: MetricsReportState;
  audit_cluster?: { last_outcome?: { passed?: boolean; [key: string]: unknown } | null; [key: string]: unknown };
  last_push?: PushStatus | null;
  // Terminal failure: escalations no longer pause for a human -- the graph ENDs with this set.
  run_failure?: EscalationPayload | null;
  stages?: {
    "app-discovery"?: StageState;
    "brownfield-baseline"?: StageState;
    "tech-stack"?: StageState;
    "raw-requirements"?: StageState;
    specification?: StageState;
    plan?: StageState;
    "ac-to-tests"?: StageState;
    "minimal-code-to-green"?: StageState;
    "adversarial-audit"?: StageState;
    "dedup-simplify"?: StageState;
    "license-audit"?: StageState;
    "exit"?: StageState;
  };
}

export type StageKey = keyof NonNullable<WorkflowState["stages"]>;

// Ordered pipeline sequence -- drives AppShell's gate label lookup (the first stage in this
// order currently "ready_for_review" is the one paused on the open interrupt) and the Session
// Overview panel's timeline. Extend this list, not a hardcoded ternary, as more gated stages land.
// Bespoke node clusters (quality-remediation/security-remediation/finding-cluster/test-hardening/metrics-report) have no StageState/gate of this shape and are
// intentionally absent here -- the Session Overview panel reads state.stages dynamically, so their
// absence from this static list doesn't hide them from that panel, only from this ordered lookup.
export const PIPELINE_STAGE_ORDER: { key: StageKey; label: string }[] = [
  { key: "app-discovery", label: "Runnable App Check" },
  { key: "brownfield-baseline", label: "Preflight Baseline" },
  { key: "tech-stack", label: "Tech Stack" },
  { key: "specification", label: "Specification" },
  { key: "plan", label: "Implementation Plan" },
  { key: "ac-to-tests", label: "Acceptance Criteria to Tests" },
  { key: "minimal-code-to-green", label: "Minimal Code to Green" },
  { key: "adversarial-audit", label: "Adversarial Audit" },
  { key: "dedup-simplify", label: "De-dup / Simplify" },
  { key: "license-audit", label: "License Audit" },
  { key: "exit", label: "Exit" },
];

// Which StageState keys each tab's status dot derives from. The quality tab has no StageState
// stages -- it reads the bespoke quality/security/test/metrics state keys directly (AppShell).
export const TAB_STAGE_GROUPS: Record<string, StageKey[]> = {
  requirements: ["raw-requirements"], // recorded as-is (always "approved"); no gate ever surfaces
  specification: ["specification"],
  plan: ["plan"],
  build: ["ac-to-tests", "minimal-code-to-green"],
  overview: [],
  quality: [],
};

export interface GatePayload {
  stage:
    | "brownfield-baseline"
    | "tech-stack"
    | "specification"
    | "plan"
    | "ac-to-tests"
    | "minimal-code-to-green"
    | "adversarial-audit"
    | "dedup-simplify"
    | "license-audit"
    | "exit";
  draft: unknown;
}

/** Escalation interrupt payloads (graph.py make_escalate_node, security gate, audit exit gate).
 * Distinct from the plain approval gate interrupt, which has no `type`. */
export interface EscalationPayload {
  stage?: string;
  type?:
    | "cannot_verify"
    | "verification_cap_exceeded"
    | "security_cycle_cap_exceeded"
    | "exit_gate_failed_twice"
    | string;
  feedback?: string;
  [key: string]: unknown;
}
