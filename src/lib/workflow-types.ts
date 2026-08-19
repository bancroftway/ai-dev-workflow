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

/** A canned monorepo stack the Tech Stack tab's dropdown offers, loaded from
 * agent/src/templates/tech_stacks/*.md via GET /api/tech-stack-catalog (agent/src/app_discovery.py's
 * load_stack_catalog). `markdown` is the full catalog file content -- picking one overwrites the
 * tab's editor with it, still hand-editable before Submit. */
export interface CannedTechStack {
  id: string;
  title: string;
  markdown: string;
}

export interface TechStackCatalogResponse {
  stacks: CannedTechStack[];
}

/** repo_scan.py's per-metric "measures" block on ScanSummary -- the metrics-bar-ready subset
 * (security worst-severity + open count, duplication/ccn/coverage numbers). Optional on
 * ScanSummary because old baseline files predate this block (see repo_scan.py's comment above
 * its recompute path) -- absence must render "--" placeholders, never crash. */
export interface ScanMeasures {
  security: {
    /** Full SEVERITY_ORDER vocabulary plus "none" for zero open security findings. "info" is a
     * real, reachable value (Trivy NONE/NEGLIGIBLE) -- graded as a B, same bucket as "low". */
    worst_open_severity: "none" | "info" | "low" | "medium" | "high" | "critical" | string;
    by_severity: Record<string, number>;
  };
  duplication_percent: number | null;
  mean_ccn: number | null;
  coverage_line_rate: number | null;
}

/** repo_scan.py's ScanReport.summary() shape -- streamed via the repo_scan state channel. */
export interface ScanSummary {
  health_score: number;
  by_severity: Record<string, number>;
  by_category: Record<string, number>;
  deduped_count: number;
  gating_count: number;
  severity_floor: string;
  measures?: ScanMeasures;
}

/** repo_scan.py's per-metric delta entry (`_metric_deltas`), keyed by metric name (e.g.
 * "health_score", "coverage_line_rate") on DeltaSummary.metrics. */
export interface MetricDelta {
  from: number;
  to: number;
  delta: number;
  direction: "improved" | "regressed" | "neutral";
}

/** repo_scan.py's `delta_summary()` shape -- a small, frontend-ready rollup of `diff_scans`'
 * full findings+metrics diff. null when there is no baseline (never a fabricated zero-delta). */
export interface DeltaSummary {
  fixed_count: number;
  introduced_count: number;
  severity_changed: number;
  net_change: Record<string, number>;
  metrics: Record<string, MetricDelta>;
  baseline_commit: string | null;
}

/** repo_scan.py's coverage shape -- `reason` is set only when `line_rate` is null (never a
 * fabricated 0). */
export interface CoverageState {
  line_rate: number | null;
  branch_rate: number | null;
  reason?: string;
}

/** Curated scan snapshot streamed on GraphState.repo_scan for the metrics bar -- small keys only,
 * full findings stay in the committed .ai-dev-workflow/repo-scan-*.json files. */
export interface RepoScanState {
  baseline?: string | null;
  baseline_summary?: ScanSummary | null;
  baseline_coverage?: CoverageState | null;
  latest_summary?: ScanSummary | null;
  latest_duplication_percent?: number | null;
  coverage?: CoverageState;
  delta_summary?: DeltaSummary | null;
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
    token_usage_summary?: { total_input_tokens: number; total_output_tokens: number; total_cost: number; by_stage?: Record<string, unknown> };
    // repo_scan.py's ScanReport.to_dashboard_dict() -- only `.summary` (the ScanSummary, same
    // shape MetricsBar reads) is used on the frontend; full findings stay in the committed files.
    repo_scan?: { summary?: ScanSummary; [key: string]: unknown };
    [key: string]: unknown;
  };
}

/** agent/src/schemas_exit.py's MergeReadinessReport -- exit's StageState.approved_content shape,
 * narrowed from `unknown` by whichever view renders it (ReportView). */
export interface MergeReadinessReport {
  merge_ready: boolean;
  blocking_reasons: string[];
  pr_title: string;
  pr_description_markdown: string;
  risk_notes: string[];
  suggested_reviewers_note?: string;
}

/** agent/src/e2e_nodes.py's E2EState -- the playwright execution stage's bespoke-cluster state.
 * Absent (undefined) until e2e_gate_check_node's first write of a given run, so every read of
 * this must be optional-chained and the screenshots section must render nothing when it's
 * undefined. */
export interface E2EState {
  status?: "running" | "passed" | "failed" | "skipped";
  attempt?: number;
  passed?: number;
  failed_tests?: { title: string; error: string }[];
  total?: number;
  cannot_verify?: boolean;
  screenshots?: string[];
  skipped_reason?: string | null;
  [key: string]: unknown;
}

/** Outcome of the last push to the single, repo-shared `ai-dev-workflow` work branch
 * (git_ops.push_head) -- every session/user on this repo pushes that same branch, via
 * --force-with-lease rather than a plain force (WS0's single-branch migration retired the old
 * per-branch `ai-dev-workflow/<branch>` naming and its "exactly one writer" invariant that made a
 * plain force safe). ok=false means GitHub persistence is currently failing (e.g. no push
 * permission) -- local commits continue regardless. */
export interface PushStatus {
  ok: boolean;
  error?: string | null;
  at?: string;
}

export interface WorkflowState {
  raw_requirements_text?: string;
  repo_scan?: RepoScanState;
  quality_remediation?: QualityRemediationState;
  security_remediation?: SecurityRemediationState;
  test_hardening?: TestHardeningState;
  metrics_report?: MetricsReportState;
  audit_cluster?: { last_outcome?: { passed?: boolean; [key: string]: unknown } | null; [key: string]: unknown };
  e2e?: E2EState | null;
  last_push?: PushStatus | null;
  // Live token spend, re-summed from the sandbox ledger whenever a background refresh scan lands
  // (agent's metrics_nodes.collect_live_refresh) -- feeds the metrics bar's Cost chip mid-run;
  // metrics_report.token_usage_summary is the final end-of-run word.
  token_usage_running?: { input_tokens: number; output_tokens: number; cost: number } | null;
  // Terminal failure: escalations no longer pause for a human -- the graph ENDs with this set.
  run_failure?: EscalationPayload | null;
  stages?: {
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
  "tech-stack": ["tech-stack"],
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
    | "e2e_cap_exceeded"
    | string;
  feedback?: string;
  [key: string]: unknown;
}
