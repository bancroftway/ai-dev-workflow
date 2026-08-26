# Pipeline prompts

Every LLM prompt in the pipeline lives here as a plain markdown file — edit freely, then restart
the agent (prompts are cached at first load via `lru_cache` in `src/prompt_loader.py`).

Naming: `<stage-id>_<purpose>.md`.
- `draft` — writes the stage's artifact.
- `audit` — adversarial second-opinion pass by a separately configured model (`agent/config/models.yaml`); the auditor **fixes the artifact directly** — its revised output replaces the draft. Only specification, plan, ac-to-tests, and minimal-code-to-green have one.
- Files with a `---` separator line are system/human pairs (`load_prompt_pair`): text above `---` is the system prompt, below is the human message template with `<<placeholder>>` tokens filled at runtime.

Raw requirements have no prompt at all: the human's text is recorded verbatim by a deterministic
node (`record_raw_requirements` in `src/graph.py`) and the specification stage does the processing.

| File | Stage | Role |
|---|---|---|
| `brownfield_baseline_draft.md` | brownfield-baseline | pre-existing system baseline |
| `tech_stack_draft.md` | tech-stack | detect languages/frameworks (Tech Stack tab's fresh-detection path) |
| `tech_stack_extract.md` | tech-stack | one-shot JSON extraction from the tab's saved/approved markdown |
| `specification_draft.md` / `specification_audit.md` | specification | user stories + acceptance criteria |
| `specification_ticket_mode_segment.md` | specification | ticket-mode: expand the existing ledger baseline, scoped to this ticket, instead of a from-scratch read |
| `plan_draft.md` / `plan_audit.md` | plan | implementation plan, diagrams, wireframes |
| `plan_greenfield_segment.md` | plan | greenfield: scaffold-first milestone segment |
| `plan_ticket_mode_segment.md` | plan | ticket-mode: extend the existing approved Plan baseline, scoped to this ticket, instead of a from-scratch read |
| `ac_to_tests_draft.md` / `ac_to_tests_audit.md` | ac-to-tests | acceptance criteria → failing tests |
| `ac_to_tests_greenfield_segment.md` | ac-to-tests | greenfield: test-scaffolding-only segment (app may not exist yet) |
| `ac_to_tests_ticket_mode_segment.md` | ac-to-tests | ticket-mode: scope test-writing to this ticket's own ACs, not the ledger's whole-project active list |
| `ac_test_run.md` | ac-to-tests | run the just-written suite and report RED/GREEN per AC |
| `minimal_code_to_green_draft.md` / `minimal_code_to_green_audit.md` | minimal-code-to-green | minimal code to pass tests |
| `minimal_code_to_green_brownfield_segment.md` | minimal-code-to-green | brownfield: extend this repo's existing code conventions instead of assuming a blank repo |
| `auth_requirement_segment.md` | specification / plan / ac-to-tests / minimal-code-to-green | enterprise auth requirement (injected when the repo's auth posture demands it AND Key Vault auth secrets are present) |
| `coverage_run.md` | minimal-code-to-green | replay the coverage contract and report the measured numbers |
| `remediation_draft.md` | remediation | fix or explain every gating finding |
| `remediation_ticket_mode_segment.md` | remediation | ticket-mode: carry an earlier ticket's own accepted `known_gaps` reasoning forward instead of re-investigating it from scratch |
| `quality_remediation_triage.md` | quality-remediation | fix/suppress decision per quality finding |
| `quality_remediation_fix.md` | quality-remediation | apply fixes + suppression markers |
| `security_remediation_triage.md` | security-remediation | fix/suppress decision per security finding |
| `security_remediation_fix.md` | security-remediation | apply security fixes |
| `adversarial_audit_draft.md` | adversarial-audit | adversarial code audit |
| `adversarial_compliance_fix.md` | adversarial-compliance | write-capable pass closing the compliance audit's divergences |
| `finding_cluster_dependency_upgrade.md` | finding-cluster | dependency upgrades |
| `finding_cluster_risk_review.md` | finding-cluster | upgrade risk review |
| `license_audit_draft.md` | license-audit | license compliance |
| `test_hardening_run.md` / `test_hardening_fix.md` | test-hardening | run the suite N× / fix the flakes it caught |
| `test_hardening_flake_triage.md` | test-hardening | flaky-test triage |
| `e2e_run.md` | e2e | app-launch discovery: start command, port, routes + API routes |
| `e2e_fix.md` | e2e | fix failing Playwright tests |
| `rebuild_verify.md` | rebuild (R placements) | verify the build/lint state before and after fixes |
| `metrics_report_ponytail_gain.md` | metrics-report | benchmark scorecard |
| `readme_draft.md` | metrics-exit (readme leg) | write/update the repo README per standard-readme |
| `memory_segment.md` | ac-to-tests / minimal-code-to-green / remediation | read + append the repo's durable memory file |
| `exit_draft.md` | metrics-exit | final summary/changelog |
| `session_title.md` | session bookkeeping | one-line session title from the requirements |
| `rebuild_build_fix.md` | rebuild (R placements) | build-failure fixes |

Not in this directory: per-placement rebuild addendums (`RebuildSpec.fix_prompt_addendum`, set
where each R placement is wired in `src/graph.py`) — placement config, not prompt copy.
