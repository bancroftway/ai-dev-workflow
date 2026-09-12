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
  /** Laps burned on report["infra_error"] verdicts (platform failed to measure) -- separate
   * budget from verify_cycle_count, capped by the agent's VERIFY_INFRA_RETRY_CAP (default 2). */
  infra_retry_count?: number;
  /** The stage's verify-lap cap, mirrored from its StageSpec by the agent's verify node so the
   * UI can say "lap N of M" without hardcoding the constant. 0/absent = not yet verified. */
  max_verify_cycles?: number;
  last_verification: StageVerification | null;
  baseline_commit: string | null;
}

/** Build phase has begun (shared by AppShell's tab gating and RequirementsView's submit lock) --
 * ac-to-tests is the first Build stage, so any status past not_started means implementation work
 * exists that a casual requirements resubmit would race. */
export function buildStarted(state: WorkflowState): boolean {
  const status = state.stages?.["ac-to-tests"]?.status;
  return status !== undefined && status !== "not_started";
}

/** The current run reached a terminal state: a recorded failure, or the exit stage approved.
 * Requirements resubmits are welcome again after either (the requirements-delta flow).
 * Both exit-stage spellings checked: the agent's live key is "metrics-exit" (stage-stable-id
 * rename), while this file's typed stages map still carries the older "exit". */
export function runEnded(state: WorkflowState): boolean {
  const stages = (state.stages ?? {}) as Record<string, StageState | undefined>;
  return state.run_failure != null || stages["metrics-exit"]?.status === "approved" || stages["exit"]?.status === "approved";
}

/** Some stage is actively drafting server-side. Unlike agent.isRunning this survives a reload
 * (it is state, not stream attachment), which is exactly when the submit lock needs it. */
export function anyStageDrafting(state: WorkflowState): boolean {
  return Object.values(state.stages ?? {}).some((stage) => stage.status === "drafting");
}

/** Mirrors agent/src/rebuild.py's RebuildState -- one placement's clean-build (+ TDD-red, for the
 * scaffold placement) loop between two real stages. Only the fields a client actually reads;
 * last_stdout_tail/last_stderr_tail/build_commands have no UI consumer. */
export interface RebuildState {
  status: "not_started" | "clean" | "failed" | "fixing";
  fix_cycle_count: number;
  last_exit_ok: boolean;
  cannot_verify: boolean;
}

export const REBUILD_STATUS_LABEL: Record<RebuildState["status"], string> = {
  not_started: "Not started",
  clean: "Passed",
  failed: "Failed",
  fixing: "Fixing",
};

/** One entry per agent/src/graph.py POST_STAGE_REBUILD placement: which real STAGES entry hands
 * off into it (row/connector goes right after that stage's own), state.rebuild's own key for it
 * (RebuildSpec.key), and the next real stage whose start means this placement is done. Covers all
 * four placements (2026-09-06 follow-up to the original ac-to-tests-only version) -- kept in one
 * list so SessionOverview.tsx/BuildView.tsx each loop over it once instead of hand-wiring four
 * near-identical call sites. */
export interface RebuildPlacement {
  afterStageKey: string;
  rebuildKey: string;
  nextStageKey: string;
  label: string;
}

export const REBUILD_PLACEMENTS: RebuildPlacement[] = [
  { afterStageKey: "ac-to-tests", rebuildKey: "r_ac_to_tests", nextStageKey: "minimal-code-to-green", label: "red-gate" },
  { afterStageKey: "minimal-code-to-green", rebuildKey: "r_minimal_code_to_green", nextStageKey: "remediation", label: "rebuild" },
  { afterStageKey: "remediation", rebuildKey: "r_remediation", nextStageKey: "adversarial-compliance", label: "rebuild" },
  { afterStageKey: "adversarial-compliance", rebuildKey: "r_adversarial_compliance", nextStageKey: "metrics-exit", label: "rebuild" },
];

/** User-reported gap (2026-09-06): a real stage approves, then Build/Overview go quiet for several
 * minutes with no row/card for it -- reads as stalled. That gap is real work: graph.py's
 * POST_STAGE_REBUILD wires a clean-build (+ TDD-red, ac-to-tests' placement only) check between
 * one stage's gate and the next one's draft (agent/src/rebuild.py's rebuild_node). The other three
 * placements' initial build-check calls all share ONE literal event stage tag ("rebuild",
 * stack_runner.run_and_report's stage_key) with each other -- SessionOverview.tsx's rebuildTimings
 * resolves that by time-windowing each placement between the two REAL stages either side of it
 * (afterStageKey's last event, nextStageKey's first) instead of trusting the shared tag alone.
 *
 * rebuild.py's own state (rb.status) only updates when the node FUNCTION RETURNS -- same lag every
 * other non-gated stage has (see BuildView.tsx's StageCard comment) -- so "running" here is
 * best-effort: this placement has been entered but the next real stage hasn't started, AND the run
 * isn't known to be idle. `runActive` is the same tri-state signal computeRunningStages/
 * isProvisional already trust: `undefined`/`null` (not loaded yet) must NOT read as "stopped",
 * only an explicit `false` does.
 *
 * `runningPhases` (root-caused 2026-09-11, "two stages active at once"): rebuild_node now emits
 * its own node_started/node_finished pair (stage=placement.rebuildKey, node="rebuild"), the SAME
 * fast run-events channel StageCard's own "Drafting"/"Auditing" label already trusts -- checked
 * here for BOTH halves of this function, each OR'd with the slower state-based signal rather than
 * replacing it (never worse than before if an event is ever dropped):
 * - "has this placement been entered at all": state.rebuild[key] only exists after rebuild_node's
 *   first RETURN, so a placement's very-first-ever attempt had NOTHING to show while genuinely
 *   running (silence read as stalled, same shape as the 2026-09-06 gap this function was built to
 *   close, just for lap zero instead of lap N).
 * - "has the NEXT stage genuinely started": state.stages[nextStageKey].status lags the run-events
 *   stream by however long a state snapshot takes to reach the client -- observed live, the
 *   connector kept showing "confirming tests fail" for several seconds after Minimal Code to
 *   Green's own StageCard had already flipped to "Drafting" from the same tab's run-events feed,
 *   i.e. two stages reading as simultaneously active from two signals that should agree.
 */
export function rebuildPhase(
  state: WorkflowState,
  placement: RebuildPlacement,
  runActive: boolean | null | undefined,
  runningPhases: Map<string, string>,
): { status: RebuildState["status"]; running: boolean } | null {
  const rb = state.rebuild?.[placement.rebuildKey];
  if (!rb && !runningPhases.has(placement.rebuildKey)) return null;
  // Same cast runEnded() above already uses: `stages` carries real backend keys (remediation,
  // adversarial-compliance, metrics-exit) this file's own typed StageState map hasn't caught up to.
  const stages = (state.stages ?? {}) as Record<string, StageState | undefined>;
  const nextStarted =
    (stages[placement.nextStageKey]?.status ?? "not_started") !== "not_started" ||
    runningPhases.has(placement.nextStageKey);
  return { status: rb?.status ?? "not_started", running: !nextStarted && runActive !== false };
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
  /** Lighthouse worst-of-routes scores (0-100), measured by e2e against the live app. Absent/null
   * for non-UI repos, runs whose e2e never produced a score, and pre-lighthouse baselines --
   * chips hide on absence, never render a zero. */
  lighthouse_performance?: number | null;
  accessibility_score?: number | null;
}

/** repo_scan.py's ScanReport.summary() shape -- streamed via the repo_scan state channel.
 * The health_* fields are v2 additions and OPTIONAL: pre-v2 baseline files (rehydrated via
 * _summary_from_stored) carry only health_score, and a scan where nothing was measurable can
 * even carry null there -- render nothing, never crash. */
export interface ScanSummary {
  health_score: number | null;
  /** v3: the weighted blend BEFORE the security-tool-coverage multiplier; health_score = round(raw * multiplier). */
  health_raw?: number | null;
  /** v3: sqrt(fraction) haircut applied to the whole score when security tools failed; 1.0 = full coverage. */
  health_coverage_multiplier?: number;
  /** v3: fraction of applicable security tools that completed (not_applicable excluded). */
  health_coverage_fraction?: number;
  /** v3: one-line derivation per measured subscore ("7 finding(s), 56.0 risk units / 50.9 kloc"). */
  health_basis?: Record<string, string>;
  /** v3: application security criticals -- display/banner only, never caps the score. */
  active_critical_count?: number;
  /** v3: authored-code kloc (scc, data/markup languages excluded) that normalizes the security leg. */
  kloc?: number | null;
  /** Per-subscore 0-100 values, null = unmeasured (its weight was redistributed). */
  health_subscores?: Record<string, number | null>;
  /** The renormalized weights the score ACTUALLY used -- ground truth for comparability. */
  health_weights_used?: Record<string, number>;
  health_score_version?: number;
  /** False when the baseline was scored under a different formula/measured set -- the UI greys
   * the baseline delta instead of presenting a meaningless comparison. */
  health_score_comparable?: boolean;
  /** Names of scan tools that failed/were missing this scan -- signals the summary is partial. */
  degraded?: string[];
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

/** agent/src/schemas.py's PresenceList -- a typed-absence list: `status: "absent"` with a real
 * `reason` is a valid, deliberate outcome (nothing to report), never conflated with an empty list
 * that could equally mean "never checked". Shared by RemediationContent and
 * AdversarialComplianceReport below. */
export interface PresenceList {
  status: "present" | "absent";
  values: string[];
  reason: string;
}

/** agent/src/schemas_remediation.py's RemediationDraftResponse -- the WHOLE response is the
 * remediation stage's report (StageSpec.content_field=None), so this is exactly
 * state.stages.remediation's draft/approved_content shape. Replaces the old
 * QualityRemediationState/SecurityRemediationState split: that shape was never actually produced
 * by the backend (root-caused 2026-09-11 -- the two stages it described were consolidated into
 * this single one, and nothing was ever updated to match, so QualityView's "Code
 * quality"/"Security" sections silently rendered nothing for every run). */
export interface RemediationContent {
  readiness: boolean;
  remediation_summary: string;
  findings_addressed: PresenceList;
  dependencies_upgraded: PresenceList;
  known_gaps: PresenceList;
}

/** agent/src/schemas_audit.py's DivergenceFinding -- one plan/code mismatch the
 * adversarial-compliance stage found. */
export interface DivergenceFinding {
  id: string;
  severity: "critical" | "major" | "minor" | "informational";
  plan_reference: string;
  description: string;
  evidence: string[];
  proposed_resolution: string;
}

/** agent/src/schemas_audit.py's AdversarialAuditReport -- state.stages["adversarial-compliance"]'s
 * draft/approved_content shape (StageSpec.content_field="report" unwraps it from the surrounding
 * AdversarialAuditDraftResponse envelope). */
export interface AdversarialComplianceReport {
  plan_conformance_summary: string;
  divergence_findings: { status: "present" | "absent"; values: DivergenceFinding[]; reason: string };
  unresolved_risk_notes: PresenceList;
  overall_verdict: "conforms" | "minor_gaps" | "major_gaps" | "fails_to_conform";
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
  // schemas_exit.py's MergeReadinessReport types both of these as PresenceList, not a bare
  // list[str] -- ReportView.tsx used to read `.length`/`.map` straight off these (root-caused
  // 2026-09-11, session 8242ea6d: a real, non-empty blocking_reasons never rendered because a
  // plain object has no `.length`, so `report.blocking_reasons.length > 0` was always `undefined`
  // and the whole "Blocking reasons" box silently never showed, even on a genuinely-blocked run).
  blocking_reasons: PresenceList;
  pr_title: string;
  pr_description_markdown: string;
  risk_notes: PresenceList;
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
  // Keyed by RebuildSpec.key (agent/src/rebuild.py) -- one entry per R placement this thread has
  // actually entered. See redGatePhase's own docstring for the one placement the UI reads today.
  rebuild?: Record<string, RebuildState>;
  stages?: {
    "brownfield-baseline"?: StageState;
    "tech-stack"?: StageState;
    "raw-requirements"?: StageState;
    specification?: StageState;
    plan?: StageState;
    "ac-to-tests"?: StageState;
    "minimal-code-to-green"?: StageState;
    // Consolidated-pipeline (stage-stable-id rename) keys -- the agent's real post-Build stages.
    // "adversarial-audit"/"dedup-simplify"/"license-audit"/"exit" below are the pre-rename keys,
    // never populated by the current graph, kept only so an old completed session's stored data
    // still resolves a label instead of a raw key.
    remediation?: StageState;
    "adversarial-compliance"?: StageState;
    "metrics-exit"?: StageState;
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
  { key: "remediation", label: "Remediation" },
  { key: "adversarial-compliance", label: "Adversarial Compliance" },
  { key: "metrics-exit", label: "Metrics & Exit" },
  // Legacy, pre-rename keys -- never populated by the current graph (see the WorkflowState
  // comment above); kept only so an old completed session's stored data still resolves a label.
  { key: "adversarial-audit", label: "Adversarial Audit" },
  { key: "dedup-simplify", label: "De-dup / Simplify" },
  { key: "license-audit", label: "License Audit" },
  { key: "exit", label: "Exit" },
];

/** Index of `key` within PIPELINE_STAGE_ORDER, or -1 for an unknown/legacy key. Purely ordinal --
 * used only to answer "has the durable current_stage moved past stage X" during the mid-run
 * reattach gap (state.stages empty), never to imply concurrent-execution semantics. */
export function stageOrderIndex(key: string | null | undefined): number {
  return PIPELINE_STAGE_ORDER.findIndex((s) => s.key === key);
}

/** Root-caused 2026-09-12: `dbo.sessions.failure_stage` is not always one of PIPELINE_STAGE_ORDER's
 * real stage keys -- a rebuild placement, e2e, test-hardening, or a metrics-regression escalate
 * all name THEIR OWN key instead (confirmed exhaustive via a repo-wide search of every
 * record_run_failure/run_failure call site in agent/src). Resolves the real, human-meaningful
 * stage a "Restart workflow from this stage" action should target. `REBUILD_PLACEMENTS`' own
 * `afterStageKey` already IS the reverse map for the four rebuild-originated keys; `e2e`/
 * `test_hardening` sit between `remediation`'s rebuild and `adversarial-compliance_draft` in the
 * graph, and `metrics_report` (metrics_nodes.py's own regression check, distinct from the
 * `"metrics-exit"` stage key) sits between `adversarial-compliance`'s rebuild and the exit stage --
 * neither has its own REBUILD_PLACEMENTS entry since neither IS a rebuild placement.
 *
 * Deliberately returns null for `"exit"` (metrics-exit's own stage approved a real report, just
 * with merge_ready=false, or the report-writing itself crashed -- the `finished_with_verdict` case
 * elsewhere in this file, which gets a "View report" link, not a restart) and for `"provisioning"`
 * (the sandbox never even booted -- current_stage stays null, no stage was ever reached to
 * restart). Callers must check `!finishedWithVerdict` before relying on this for a failed session. */
export function realStageForFailure(failureStage: string | null | undefined): string | null {
  if (!failureStage) return null;
  if (stageOrderIndex(failureStage) >= 0) return failureStage;
  const placement = REBUILD_PLACEMENTS.find((p) => p.rebuildKey === failureStage);
  if (placement) return placement.afterStageKey;
  if (failureStage === "e2e" || failureStage === "test_hardening") return "remediation";
  if (failureStage === "metrics_report") return "adversarial-compliance";
  return null;
}

// Which StageState keys each tab's status dot derives from. Quality's own status dot is still
// computed from the bespoke quality/security/test/metrics state keys directly (AppShell) -- but
// remediation/adversarial-compliance ARE real StageState-shaped stages under the consolidated
// pipeline (agent/src/graph.py), so they belong here for tabForStage's reverse lookup (AppShell) to
// resolve a mid-run reattach onto the Quality tab instead of silently no-opping.
export const TAB_STAGE_GROUPS: Record<string, StageKey[]> = {
  "tech-stack": ["tech-stack"],
  requirements: ["raw-requirements"], // recorded as-is (always "approved"); no gate ever surfaces
  specification: ["specification"],
  plan: ["plan"],
  build: ["ac-to-tests", "minimal-code-to-green"],
  overview: [],
  quality: ["remediation", "adversarial-compliance"],
};

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
    | "draft_infra_exhausted"
    // Not a failure -- the Specification stage's zero-net-delta gate (graph.py
    // make_no_new_work_node): the draft passed its ledger sync, but classifies zero new/modified/
    // deleted US/AC versus the last approved baseline, so the run ends before the human gate.
    | "no_new_work"
    | string;
  // WHY the run died (a real gate-verified defect vs. a quota/timeout/infra failure) --
  // orthogonal to `type` above (WHICH ceiling was hit). See agent/src/failure_classification.py.
  failure_type?: "gate_exhausted" | "infra_transient" | "quota_exhausted" | string;
  feedback?: string;
  [key: string]: unknown;
}
