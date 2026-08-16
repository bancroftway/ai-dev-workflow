# ai-dev-workflow

A human-gated, LLM-driven software delivery pipeline built as a single [LangGraph](https://langchain-ai.github.io/langgraph/) state graph. Every stage drafts an artifact and runs a *deterministic* check (a real script or parse — never LLM self-attestation). Exactly three stages get an adversarial second-model audit (specification, plan, minimal-code-to-green) and exactly two pause for a human (specification, plan) — every other failure ENDs the run with a `run_failure` record instead of waiting on a person. All work happens inside a per-session sandbox container holding a clone of the target repo/branch.

- Graph definition: [agent/src/graph.py](agent/src/graph.py)
- Frontend (AG-UI / CopilotKit): [src/](src/)
- Plan of record: [docs/PLAN.md](docs/PLAN.md)

---

## The whole graph, start to finish

Each box is one stage. The title says what the stage is for; the numbered lines are the operations it performs in order, including the skills and MCP servers it calls.

**Edge legend** — solid: normal flow · dotted: retry / loop-back · `human` label: the graph pauses on a LangGraph `interrupt()` until a person resolves it (only specification and plan have one). An exhausted retry cap anywhere else ENDs the run: the failure is written to the ledger, committed, pushed, and surfaced as `run_failure` in state — resubmitting starts a fresh attempt with counters reset.

The pipeline opens with a suitability gate. `ai-dev-workflow` only applies to a repository containing a startable web app, API, or Azure Function; a library, a package, or a mobile-only repo is rejected with reasons, and the run ends there — the only hard stop in the graph. It runs before anything is written to the repository, so a rejected repo is left exactly as it arrived.

```mermaid
flowchart TD
    session["SESSION PROVISIONING &nbsp;·&nbsp; before the graph is ever invoked (agent/src/sessions_api.py)<br/>1. Next.js server route calls POST /sessions/provision with thread_id, owner, repo, branch<br/>2. sandbox factory picks a provider: local Docker or Azure ACI (agent/src/sandbox/)<br/>3. provider clones owner/repo at branch into /workspace/repo (a per-session named volume locally — the tree and any unpushed commits survive container removal; explicit session DELETE discards it), then checks out the tool-owned work branch ai-dev-workflow/&lt;branch&gt;-&lt;session prefix&gt; (session-suffixed so two users of the same repo+branch never share one force-pushed remote branch; reused from the volume when present, fetched from origin on brownfield re-entry, created fresh otherwise). The user's selected branch is never committed on. Copilot CLI token injected as env; the git clone token arrives as a one-shot pre-start file (never visible in docker inspect); per-owner package cache mounted at /opt/aidw/cache<br/>4. entrypoint runs bootstrap.sh: installs any toolchain the repo declares for itself (.tool-versions, mise.toml, .nvmrc, global.json) into /opt/aidw/tools — non-fatal, and never into the repo<br/>5. registry.set(thread_id, session) plus registry.set_meta(user_login, target_branch, resume) — the GitHub login and `?resume=1` flag the Next.js route forwarded, both consumed later (session_index.py, intake); the GitHub token is retained agent-memory-only for stage-end pushes (git_ops.push_head) — every later node checks this registry before touching disk<br/>6. frontend does agent.addMessage(requirements) then runAgent() — this is what starts the graph"]

    intake["INTAKE &nbsp;·&nbsp; normalize the run and decide what carries over from previous runs<br/>1. mint a fresh run_id (used by the spec ledger, APPROVALS.md and metrics-report/exit snapshots)<br/>2. pop the registry's one-shot `resume` meta flag unconditionally (it must never leak into a later, unrelated run)<br/>3. compare the latest HumanMessage's id against consumed_message_id (a state channel): a DIFFERENT id is a fresh submission (a real chat message, including a clarification answer) — SAME id (or no HumanMessage at all) means this run is textless, because a live-thread Resume click fires a blank runAgent() with no new message and add_messages/the checkpointer replay the SAME old message id every time; text presence alone can't tell these apart, since the checkpoint still ends in the ORIGINAL non-empty HumanMessage either way<br/>4. first invoke for this thread: hydrate every stage's state back out of the repo (workflow_persistence.py)<br/>5. seed default state for every StageSpec that has none yet<br/>6. reset specification onward to not_started — tech-stack and raw-requirements stay approved across runs; AIDW_RESUME=1 or the resume flag (only when textless, per step 3) skips this reset instead, so a resume picks up at the first unapproved stage (in-memory checkpoint or repo hydration, whichever this thread has)<br/>7. a textless run falls back to the hydrated raw-requirements doc, so a resume never drafts the spec from nothing<br/>8. a blank run with no requirements anywhere (the frontend's reload/reattach ping) ends here — zero LLM calls"]

    scaffold["SCAFFOLD &nbsp;·&nbsp; read-mostly entry point (preflight_nodes.py)<br/>1. reset the workflow action ledger (fresh per session)<br/>2. session_index.start_session: UPSERT this thread's row in .ai-dev-workflow/sessions.json (title = first line of the requirements text) and commit+push immediately — BEFORE the baseline capture below, so a later reject's hard reset can't erase it<br/>3. capture git rev-parse HEAD as this run's baseline — the point the reject path resets back to<br/>4. read .ai-dev-workflow/manifest.json — its absence is the canonical never-onboarded-before signal<br/>Nothing else is written to the repo here. The repo-visible writes wait until the suitability gate passes"]

    apre["APP DISCOVERY PRE &nbsp;·&nbsp; deterministic scan for startable applications (app_discovery.py)<br/>1. one bounded find for marker files: *.csproj, host.json, package.json, launchSettings.json, Program.cs, Dockerfile, manage.py, pyproject.toml, app.json, capacitor/ionic configs<br/>2. bounded reads: 60 files max, 4000 chars each, 24000-char evidence blob<br/>3. classify_candidates (pure): web SDK, Functions SDK, framework dependency, or negative evidence (library, no start script)<br/>4. fingerprint over path AND content hashes — the staleness signal for the next run's hydration"]

    app["APP DISCOVERY &nbsp;·&nbsp; does this repo contain an app this workflow can run?<br/>1. hydrate short-circuit: skip the LLM when the manifest already records an accepted result at this exact fingerprint<br/>2. draft: read-only tools, grounded in the scan but free to explore past it — the marker table has no Go/Rails/Spring/PHP rules and a false reject is unrecoverable<br/>3. no audit, no human gate — the deterministic decision below is the gate (it drops any cited path that does not exist)"]

    decide["APP DISCOVERY DECIDE &nbsp;·&nbsp; the verdict, deterministic and fail-closed<br/>1. drop any app whose cited path does not exist<br/>2. suitable = at least one web / api / azure_function app, on dotnet/node/python, with a real start command<br/>3. mobile is detected and rejected on purpose — the sandbox is a Linux container with no Android SDK, JDK/Gradle or Xcode<br/>4. no report at all is a rejection whose reason names that honestly, rather than blaming the repo<br/>5. reasons are composed from what was actually found, never from the model's own suitable flag"]

    reject["REJECT &nbsp;·&nbsp; the one hard stop in the graph<br/>1. post the reasons as a chat message and into shared state (the red banner in Requirements)<br/>2. verify every commit since the run baseline is the workflow's own; if not, skip the reset and say so<br/>3. git reset --hard to the baseline, git clean -fd .ai-dev-workflow<br/>4. close this run's sessions.json row as rejected and commit+push it — the one writer that runs AFTER the reset above, so it needs its own commit rather than riding another node's<br/>5. END — the repo is left exactly as it arrived"]

    sfin["SCAFFOLD FINALIZE &nbsp;·&nbsp; the write half of scaffolding, deferred until the repo is accepted<br/>1. write AGENTS.md and a thin .github/copilot-instructions.md pointer if absent — never overwriting a human-authored one<br/>2. if AGENTS.md already exists, append only the pointer paragraph to .ai-dev-workflow/tech-stack.md, so a hand-written file still leads agents to the conventions<br/>3. fold agent-work/toolchain-bootstrap.json into .ai-dev-workflow/manifest.json, .ai-dev-workflow/ledger.jsonl and the host-side toolchain log<br/>4. commit them"]

    record["APP CHECK RECORD &nbsp;·&nbsp; persist the accepted apps<br/>1. read-modify-write app_check into .ai-dev-workflow/manifest.json: class, runtime, start command, port, evidence, fingerprint<br/>2. commit<br/>Placed after both branches converge on purpose: creating the manifest earlier would let a run abandoned mid-baseline skip brownfield ratification forever"]

    rscan["REPO SCAN BASELINE &nbsp;·&nbsp; measure the repository exactly as it arrived (repo_scan.py)<br/>1. run the full licence-vetted tool set offline: scc, lizard, jscpd, gitleaks, trivy, osv-scanner, semgrep, git churn — the summary streams into shared state (baseline_summary) to light the frontend metrics bar<br/>2. normalize every result into one Finding vocabulary and deduplicate across tools — trivy and osv-scanner name the same advisory differently, and OSV's alias lists are what reconcile them<br/>3. write .ai-dev-workflow/repo-scan-baseline.json and commit it<br/>4. idempotent on that file, and that is a correctness requirement: every node here is re-entered on every clarification round, and re-baselining would silently zero out the improvement the metrics-report delta exists to report<br/>Placed after both branches converge and before raw-requirements, so the clone exists and the stack is known but nothing has written application code yet"]

    p0pre["BROWNFIELD PRE &nbsp;·&nbsp; brownfield grounding (only when manifest.json is missing)<br/>1. deterministic grep of the repo for schemas, migrations and route definitions<br/>2. store the result as brownfield_context, so the baseline draft is grounded in facts rather than guesses"]

    p0["BROWNFIELD BASELINE &nbsp;·&nbsp; describe the existing system before changing it<br/>1. draft: read-only tool allowlist — skills: preflight-baseline, tech-stack-conventions, caveman<br/>2. no audit, no human gate — ratification is automatic<br/>3. brownfield_write_manifest: what actually creates .ai-dev-workflow/manifest.json"]

    ts["TECH STACK &nbsp;·&nbsp; detect languages, frameworks and build/test commands once per repo<br/>1. hydrate short-circuit: if .ai-dev-workflow/tech-stack.approved.json already exists, mark approved and skip the LLM entirely<br/>2. draft: read-only tool allowlist — skill: tech-stack-conventions<br/>3. no audit, no human gate — supporting infrastructure, it has no review tab<br/>4. post-approve hook: write each detected ecosystem's build-blocking config and append one paragraph per ecosystem to AGENTS.md — .NET gets &lt;solution-root&gt;/Directory.Build.props, Python gets &lt;root&gt;/ruff.toml and &lt;root&gt;/mypy.ini; Node/TS gets NOTHING written into the repo — its lint toolchain is baked into the sandbox image at /opt/aidw/lint and the rebuild gate runs it from there (a repo with its own ESLint setup keeps its own lint contract)<br/>Runs on the approved path, not post-audit, so it still fires on the hydrate short-circuit — otherwise a repo onboarded once would never receive a new or updated convention<br/>Everything downstream reads this: build commands, test commands, and whether Playwright/Excalidraw MCP get attached"]

    rr["RECORD RAW REQUIREMENTS &nbsp;·&nbsp; the human's text, accepted exactly as written<br/>1. deterministic, zero LLM calls: the submitted text is recorded as the approved requirements verbatim — specification is the stage that processes it<br/>2. persists .ai-dev-workflow/raw-requirements.md (+ .approved.json) and commits<br/>3. a blank submission keeps the previously hydrated requirements untouched"]

    spec["SPECIFICATION &nbsp;·&nbsp; user stories and acceptance criteria with permanently stable ids<br/>1. draft: requirements text plus any attachments (screenshots/documents) — skill: spec-sync<br/>2. audit: adversarial revision — skills: ponytail (prose), spec-sync<br/>3. verify (deterministic): sync every US/AC id against .ai-dev-workflow/spec/ledger.json (committed by the verify-pass persist)<br/>4. gate: human approval<br/>5. sign: append a content-hash-signed row to APPROVALS.md so later tampering is detectable"]

    plan["IMPLEMENTATION PLAN &nbsp;·&nbsp; ordered steps plus diagrams, derived only from the approved spec<br/>1. draft: input is the approved Specification, never the raw requirements — UI-framework repos must also emit one self-contained HTML wireframe per new/changed screen (max 6, 30 KB each; inline CSS only, no scripts, no external URLs) and get impeccable `shape` methodology (read-only, no scripts)<br/>2. audit: adversarial revision — skill: ponytail (prose); also reviews and FIXES the wireframes against the spec (the auditor revises artifacts directly, there is no separate editor)<br/>3. verify (deterministic): validate every wireframe (name, size, self-containment — pure checks, no Chromium) and render every Mermaid diagram with mmdc inside the sandbox; a render failure is a syntax failure. Both are committed to .ai-dev-workflow/plan/ on pass<br/>4. gate: human approval<br/>5. sign: content-hash-signed row in APPROVALS.md"]

    p4["AC TO TESTS &nbsp;·&nbsp; write the failing tests first (TDD red), touching test files only<br/>1. capture baseline commit (git rev-parse HEAD) — the reference point for the write-scope check<br/>2. draft: autopilot write access, bash excluded, PreToolUse write-scope hook armed, Playwright MCP for UI repos — skills: ac-to-tests, test-driven-development<br/>3. verify (deterministic), both halves must pass:<br/>&nbsp;&nbsp;&nbsp;&nbsp;a. write-scope gate — git diff against the baseline commit, every changed path must be a test path (pipeline-owned artifacts — .ai-dev-workflow/, APPROVALS.md, AGENTS.md — are exempt: the workflow itself commits those mid-stage)<br/>&nbsp;&nbsp;&nbsp;&nbsp;b. AC-coverage gate — every active AC has a test whose name embeds its id, and that test is currently FAILING<br/>4. no audit, no human gate — the deterministic gate is the gate"]

    r4["R · REBUILD (scaffold-only fix) &nbsp;·&nbsp; the tree must still compile after new tests land<br/>1. run the stack's plain build command; exit code is the whole gate — no LLM in the happy path<br/>&nbsp;&nbsp;&nbsp;compile-only on purpose: strict lint/typecheck would flag pre-existing repo debt this placement's fixer is forbidden to touch — the full strict gate runs at the post-codegen R placements instead<br/>2. on failure, fix node may add compile-enabling stubs only, never real behavior — skill: systematic-debugging<br/>3. up to 3 fix cycles, then the run ENDs with run_failure"]

    p6["MINIMAL CODE TO GREEN &nbsp;·&nbsp; write the least code that turns the ac-to-tests tests green<br/>1. draft: autopilot, full unscoped write access — skills: executing-plans, subagent-driven-development, ponytail (ultra, ADVISORY: Copilot arbitrates each suggestion, implements only what it agrees with, records rejections in ponytail_rejected); UI-framework repos also get impeccable design rules plus a one-time PRODUCT.md/DESIGN.md bootstrap from the approved spec<br/>2. audit: read-only allowlist — also reviews the ponytail arbitration itself<br/>3. verify (deterministic): 95% line+branch coverage via CONTRACT REPLAY — the draft records working coverage command(s) per stack in .ai-dev-workflow/coverage-commands.json (it owns the HOW); the gate deletes each artifact, re-executes each command ITSELF, parses only standard formats (cobertura / istanbul json-summary), and merges counts line-weighted across stacks (it owns the NUMBER — no model-reported figure is ever read). Plus an anti-gaming check that coverage-exclusion config was not broadened<br/>4. no human gate — the coverage verify is the gate"]

    r6["R · REBUILD (full fix) &nbsp;·&nbsp; clean build after real implementation work<br/>1. clean+build, gate on exit code<br/>2. on failure, full-scope fix — skill: systematic-debugging (4-phase root-cause analysis)<br/>3. up to 3 fix cycles, then the run ENDs with run_failure"]

    p8["QUALITY REMEDIATION &nbsp;·&nbsp; analyzer findings triaged, fixed or explicitly suppressed<br/>1. quality_scan: dotnet build with SARIF ErrorLog and dotnet format --verify-no-changes, plus repo_scan's quality profile (jscpd duplication, lizard per-function complexity) — also refreshes repo-scan-latest.json and streams the summary to the metrics bar<br/>2. quality_triage: LLM decides fix-or-suppress per finding — skill: quality-triage<br/>3. quality_ledger_write: every suppression gets a written justification — no silent suppression<br/>4. quality_fix: dotnet format plus LLM fixes for what triage marked fixable — then git add -A commit + push (commit_all)<br/>5. R(quality): clean rebuild after the fixes<br/>6. quality_gate_check: analyzer errors and the duplication threshold gate absolutely; complexity findings gate only if they are NEW against the baseline scan, so a brownfield repo's inherited debt is reported and burned down rather than deadlocking its first gate<br/>7. pass, or loop back to scan (max 3 cycles), then the run ENDs with run_failure"]

    p10["SECURITY REMEDIATION &nbsp;·&nbsp; same shape as P8, tuned for vulnerabilities and secrets<br/>1. security_scan: repo_scan's security profile — semgrep against vendored rules, trivy for vuln/misconfig/license/secret, gitleaks, osv-scanner — all fully offline against databases baked into the image, deduplicated across tools, plus a CycloneDX SBOM; refreshes repo-scan-latest.json and streams the summary to the metrics bar<br/>2. security_triage: fix-or-suppress per finding — skills: security-triage, security-review — a secret can NEVER be suppressed, enforced on the finding's category rather than on which tool reported it<br/>3. security_ledger_write: justification recorded for every suppression<br/>4. security_fix: LLM fixes the findings triage marked fixable — then git add -A commit + push (commit_all)<br/>5. R(security): clean rebuild<br/>6. security_gate_check: absolute, not delta-scoped — an inherited CVE is still exploitable. Zero unsuppressed findings at or above the severity floor (default: medium), else loop (max 3), then the run ENDs with run_failure"]

    p11a["ADVERSARIAL AUDIT &nbsp;·&nbsp; does the code that now exists actually match the spec and plan?<br/>1. draft: compare approved Specification and Plan against the real repo, report divergences — skills: caveman, verification-before-completion; UI-framework repos also get an impeccable `critique`-style design review (read-only, no scripts), findings folded into the report<br/>2. no audit, no human gate — findings flow into de-dup and the audit exit gate's objective re-checks"]

    p11b["DE-DUP / SIMPLIFY &nbsp;·&nbsp; collapse the duplication the pipeline just introduced<br/>1. dedup_simplify_pre: run jscpd through repo_scan, feed the parsed clone pairs into the draft prompt<br/>2. draft: autopilot write access, refactor the clusters — jscpd findings are authoritative; ponytail ultra + ponytail-audit run as ADVISORY proposals Copilot arbitrates (rejections recorded in ponytail_rejected); UI-framework repos also run impeccable's deterministic design detector and an impeccable `polish` pass over adversarial-audit's design findings<br/>3. no audit, no human gate — jscpd's objective re-check at the audit exit gate is the real bound<br/>4. post-approve hook: re-run jscpd and record the new duplication percentage"]

    p11c["FINDING CLUSTER (DEPENDENCY UPGRADES) &nbsp;·&nbsp; verify-before-audit, because a bad upgrade is objectively detectable<br/>1. finding_cluster_pre: list outdated dependencies with the stack's own command<br/>2. finding_cluster_draft: write access — perform upgrades and regenerate lockfiles<br/>3. finding_cluster_verify: clean rebuild plus full test run<br/>4. pass, then finding_cluster_audit — read-only risk review of the upgrade<br/>5. fail with cycles left, then loop back to draft carrying the failure evidence<br/>6. fail at the cap, then finding_cluster_revert (git revert) and a logged notice (no interrupt) that never blocks the audit cluster"]

    p11d["LICENSE AUDIT &nbsp;·&nbsp; classify every dependency license against policy<br/>1. license_audit_pre: deterministic license scan, declared and detected licenses per package<br/>2. draft: classify each package against license-policy.json — skill: license-audit<br/>3. verify (deterministic): any flagged package ENDs the run immediately (max_verify_cycles is 0 — redrafting cannot change a license); the failure text says to remove or replace the flagged dependency, then resubmit"]

    p11exit["AUDIT EXIT GATE &nbsp;·&nbsp; re-prove the objective properties instead of trusting earlier stages<br/>1. re-verify test coverage against the threshold<br/>2. re-verify duplication below the max percentage (default 3%)<br/>3. re-verify license policy and write THIRD-PARTY-NOTICES.md<br/>4. pass, or retry once, then the run ENDs with run_failure"]

    r11["R · REBUILD (full fix) &nbsp;·&nbsp; clean build after all of the audit cluster's refactoring and upgrades"]

    p13["TEST HARDENING · FULL TEST SUITE + FLAKE QUARANTINE<br/>1. test_hardening_run_tests: run the whole suite with retries; parse trx (.NET) or vitest JSON (JS/TS)<br/>2. any stable failure, then test_hardening_regression_gate — the run ENDs with run_failure (out of test-hardening's scope to fix)<br/>3. test_hardening_flake_triage: narrow read-only LLM judgment over the intermittent failures<br/>4. test_hardening_mint_tickets: allocate real US-#### ids through spec_ledger.py — deterministic, never the LLM<br/>5. test_hardening_exit_check: every quarantined test is linked to a ticket, else one retry through triage, then the run ENDs with run_failure"]

    p14["METRICS REPORT + TRACEABILITY &nbsp;·&nbsp; deterministic, with exactly one named LLM exception<br/>1. run repo_scan's full profile: size and language mix, per-function complexity, duplication, churn/hotspots/ownership, and every deduplicated security finding with its CVE and fix version<br/>2. diff it against the baseline taken at the top of the graph — what was fixed, what was introduced, what got worse, and each metric's direction declared rather than inferred (more code is neutral, more duplication is a regression)<br/>3. read the coverage summary<br/>4. build traceability-matrix.md by matching AC ids embedded in test names back to the ledger<br/>5. sum token consumption from every stage's ledger entries<br/>6. write repo-scan-latest.json, repo-scan-delta.json and metrics-latest.json<br/>7. metrics_ponytail_gain: the one LLM call — run /ponytail-gain for the code/cost/speed scorecard<br/>No baseline recorded means the delta is omitted with a reason, never fabricated as a zero"]

    p15["EXIT &nbsp;·&nbsp; is this actually merge-ready?<br/>1. draft: merge-readiness report and PR description from the spec, plan and metrics-report metrics — skills: caveman, finishing-a-development-branch<br/>2. no audit, no human gate — the report is signed AS-DRAFTED (no second-model review); specification and plan are the only two human checkpoints<br/>3. sign: content-hash-signed row in APPROVALS.md<br/>4. exit_finalize (deterministic): update manifest.json, write the CHANGELOG entry from the ledger diff, close this run's sessions.json row as completed (merge_ready, pr_title)<br/>5. write per-run exit report artifacts: history/&lt;run_id&gt;-report.json (merge readiness, metrics, delta vs baseline, files/commits diff, screenshots) and history/&lt;run_id&gt;-exit.md, read by the frontend Report tab and the past-session report page (raw screenshots served through a hardened same-origin proxy)<br/>6. prune history/ artifacts of runs older than the last AIDW_HISTORY_RETAIN (default 10), keeping every run sessions.json still lists plus this run, then commit"]

    pause(["PAUSE FOR HUMAN INPUT<br/>Any draft that comes back not-ready emits clarifying questions and ends the run.<br/>The human answers in chat, and the next run re-enters at INTAKE from the top.<br/>(Headless mode forbids this: drafts must make and record assumptions instead.)"])

    failed(["RUN FAILURE — END<br/>Any exhausted retry cap or lost sandbox ends the run here:<br/>ledger row written, committed and pushed; run_failure streamed to the UI; this run's sessions.json row closed as failed (stage, type, message).<br/>Counters reset in the same step, so resubmitting starts a clean attempt. The /select history UI's Resume button re-enters at INTAKE with ?resume=1."])

    done(["END"])

    session --> intake --> scaffold --> apre --> app --> decide
    decide -->|unsuitable| reject --> done
    decide -->|suitable| sfin --> ts
    ts -->|manifest.json exists| record
    ts -->|no manifest.json| p0pre
    p0pre --> p0
    p0 --> record
    record --> rscan --> rr
    rr --> spec
    spec -->|human| plan
    plan -->|human| p4
    p4 --> r4 --> p6
    p6 --> r6 --> p8
    p8 --> p10 --> p11a
    p11a --> p11b --> p11c --> p11d --> p11exit --> r11 --> p13
    p13 --> p14 --> p15
    p15 --> done

    p4 -.->|write-scope or AC-coverage failure, 3 tries| p4
    p6 -.->|coverage below threshold, 3 tries| p6
    spec -.->|ledger sync failure| spec
    plan -.->|diagram render failure| plan
    p8 -.->|gate not met, max 3 cycles| p8
    p10 -.->|gate not met, max 3 cycles| p10
    p11exit -.->|retry once| p11exit
    p13 -.->|unlinked quarantine, one retry| p13
    p4 -.->|cap| failed
    p6 -.->|cap| failed
    p8 -.->|cap| failed
    p10 -.->|cap| failed
    p11d -.->|flagged license| failed
    p11exit -.->|cap| failed
    p13 -.->|stable failure or cap| failed
    r4 -.->|cap| failed
    r6 -.->|cap| failed
    r11 -.->|cap| failed
    ts -.-> pause
    spec -.-> pause
    plan -.-> pause
    p6 -.-> pause
```

## Every file this pipeline writes into a target repo

| Path | Written by | Purpose |
|---|---|---|
| `AGENTS.md` | scaffold finalize, tech-stack | Cross-tool agent guidance. Created only if absent; an existing one is never overwritten, only appended to (one sentinel-guarded paragraph pointing at `.ai-dev-workflow/tech-stack.md`, plus one per detected ecosystem). |
| `.github/copilot-instructions.md` | scaffold finalize | Thin pointer to `AGENTS.md`. Created only if absent. |
| `.ai-dev-workflow/manifest.json` | brownfield-baseline, app discovery, exit, scaffold finalize | Onboarding state, accepted app record, run summary, and the `toolchain` record. Co-owned — every writer goes through one read-modify-write helper. |
| `.ai-dev-workflow/tech-stack.md` | tech-stack | The detected stack, rendered. **This is the file `AGENTS.md` tells every agent to read first.** |
| `.ai-dev-workflow/tech-stack.approved.json` | tech-stack | Typed sidecar. Its presence is what makes a later run skip detection entirely. |
| `.ai-dev-workflow/raw-requirements.md`, `specification.md`, `plan.md` | record raw requirements, specification, plan | The reviewed artifacts (raw requirements are recorded verbatim, never redrafted). |
| `.ai-dev-workflow/spec/ledger.json` | specification verify | Permanently stable US/AC ids — the sync target for every later traceability check. |
| `.ai-dev-workflow/plan/diagrams/*.{mmd,svg}` | plan verify | Rendered Mermaid diagrams. |
| `.ai-dev-workflow/plan/wireframes/*.html` | plan verify | Self-contained HTML wireframes (UI plans only) — open directly in a browser. |
| `.ai-dev-workflow/coverage-commands.json` | minimal-code-to-green | The coverage contract: per-stack command + artifact + format. Written by the draft, REPLAYED by the coverage gate — the gate deletes artifacts and re-runs each command itself, so the number is always machine-derived. |
| `.ai-dev-workflow/ledger.jsonl` | every node | Per-session action log, including token usage and toolchain installs. Reset each session. |
| `.ai-dev-workflow/repo-scan-baseline.json` | repo scan baseline | The repository as it arrived, measured once and never re-measured. Delete it to force a re-baseline. |
| `.ai-dev-workflow/repo-scan-latest.json` | metrics-report | The same shape at the end of the run: deduplicated findings with severity, location, CVE and fix version, plus size/complexity/duplication/churn metrics and a health score. Findings carry no tool attribution — which tool found what lives in the report's `tools[]` run-health block. |
| `.ai-dev-workflow/repo-scan-delta.json` | metrics-report | Baseline versus latest: fixed, introduced, persisted, severity changes, and per-metric direction. Omitted, not faked, when no baseline exists. |
| `.ai-dev-workflow/metrics-latest.json` | metrics-report | The scan and its delta, plus coverage, traceability and token totals. |
| `<solution-root>/Directory.Build.props` | tech-stack | .NET analyzers + `TreatWarningsAsErrors`. |
| `<python-root>/ruff.toml`, `<python-root>/mypy.ini` | tech-stack | Shared ruff + mypy baseline. |

The last two rows exist for one reason: an LLM reliably fixes what a deterministic tool *refuses to accept*, and treats everything else as advice. Each config is paired with a build command that fails on violation (see the R · REBUILD boxes above), which is what turns a lint finding into work the agent must complete.

Node/TS gets the same enforcement **without any file or dependency landing in the repo**: the ESLint toolchain (config + pinned plugins) is baked into the sandbox image at `/opt/aidw/lint` and the full-scope rebuild gates run it from there. Installing lint devDependencies into a target repo is a mistake this pipeline made once — the root install re-resolved a pnpm workspace's peer graph, forked `drizzle-orm` into two incompatible peer-variant instances, and broke the repo's own build.

Two caveats worth stating rather than burying:

- **Severity is not uniform across ecosystems.** .NET stays at `AnalysisLevel=latest-recommended`, the setting already in production use. Node/TS (`typescript-eslint` strict) and Python (`mypy --strict`) start stricter. The strict lint/typecheck gates therefore run only at the full-scope R placements (post-codegen), where the fixer may refactor anything — the scaffold-only placement gates on compilation alone.
- **A repo that lints its own way keeps its own lint contract.** Any ESLint config of the repo's own (any `.eslintrc*`, any `eslint.config.*`) makes the pipeline defer entirely — the image-baked config only ever applies to repos with no lint setup at all.

A file already present and not written by this pipeline is left alone. Our own files carry a version stamp in their header and are replaced when the bundled template moves forward; a file without that header is treated as human-authored.

## The sandbox filesystem

The image is immutable and deliberately small, so a repo needing a toolchain it doesn't ship installs one at runtime — never into the source tree.

| Path | Contents | Lifetime |
|---|---|---|
| `/workspace/repo` | the clone | the session. Never a mount target. |
| `/opt/aidw/tools` | mise-installed SDKs and anything else on `PATH` | the session — an executable on `PATH` is what would carry an attack between two sessions, so it is never shared |
| `/opt/aidw/cache` | npm / NuGet / pip / uv / mise download caches | a named Docker volume per repo **owner** (`aidw-cache-<owner>`) |

Both `/opt/aidw` paths are created and declared in the image itself, so a container behaves identically with or without the volume attached — the mount is an accelerator, never a correctness dependency. On Azure ACI the cache is an Azure Files share and is **off unless `AIDW_CACHE_SHARE` is set**: SMB's many-small-file throughput is poor enough that the cache can be slower than re-downloading, so it gets enabled after measurement.

[agent/sandbox-image/bootstrap.sh](agent/sandbox-image/bootstrap.sh) runs after the clone and after the git credentials are destroyed. It installs only what the repo declares for itself (`.tool-versions`, `mise.toml`, `.nvmrc`, `.node-version`, `global.json`), only from mise's own registry — a config naming an arbitrary plugin git URL is refused, since that is third-party shell that would otherwise run automatically before anything else in the container. Failure is never fatal: a missing toolchain surfaces later as a real build error, which beats a container that refuses to start. `apt-get` at runtime is impossible by construction (the container runs as non-root `vscode`); a genuine OS-package need is a `BASE_IMAGE` change.

What it found is recorded three ways: `.ai-dev-workflow/manifest.json`'s `toolchain` key (durable per repo, rewritten only when the tool set actually changes, and used for a warm start next run), `.ai-dev-workflow/ledger.jsonl` (this run's install metrics), and a host-side `agent/agent-work/toolchain.jsonl` (`$AIDW_TOOLCHAIN_LOG`). The host-side log is the one that answers "what should the next image ship" — commits are pushed to the single `ai-dev-workflow` work branch at every stage end, so the in-repo copy survives the container.

## The stage template every box shares

Most of the boxes above are the same generated subgraph, built from one `StageSpec` entry in [agent/src/graph.py](agent/src/graph.py). Adding a stage means adding a spec, not rewiring the graph.

Every LLM prompt in the pipeline is an editable markdown file under [agent/src/prompts/](agent/src/prompts/) — see its README for the file-to-stage index. Edit a prompt, restart the agent, done (prompts are cached at first load).

```mermaid
flowchart LR
    d["DRAFT<br/>LLM produces the artifact.<br/>Optional short-circuits: hydrate from an<br/>existing repo file, or capture a baseline commit."]
    a["AUDIT<br/>A separately configured model revises<br/>the draft adversarially. Optional — only<br/>specification, plan and minimal-code-to-green<br/>configure one; every other stage goes<br/>straight from draft to verify/gate."]
    v["VERIFY<br/>A real script or parse.<br/>Never LLM self-attestation.<br/>Optional per stage."]
    g["GATE<br/>LangGraph interrupt() pauses<br/>here until a human approves.<br/>Only specification and plan set<br/>requires_human_gate."]
    aa["AUTO-APPROVE<br/>Clarification-cycle safety cap hit:<br/>proceed as if approved, skipping the audit."]
    e["ESCALATE<br/>Verify cap exhausted. The run ENDs with<br/>run_failure recorded (ledger + commit + push).<br/>Never auto-approved past a failed<br/>deterministic gate. Counters reset for resubmit."]
    q(["Not ready: emit clarifying questions, end the run"])

    d -->|readiness| a
    d -->|cap reached| aa
    d -->|not ready| q
    a --> v
    v -->|passed| g
    v -.->|failed, retries left| d
    v -->|failed at cap| e
    e --> theend(["END"])
    g --> next["next stage"]
    aa --> next
```

Cross-cutting behavior that is not drawn above, because it happens in nearly every node: state is persisted to the sandbox repo and committed after each audit, verify and gate — and every successful commit is pushed to the single, repo-shared `ai-dev-workflow` work branch on origin (`--force-with-lease`, not plain `--force` — WS0's single-branch migration means every session/user on a repo shares this one branch, so a losing race is rejected instead of silently overwriting another session's already-pushed commits; a failed push is logged, surfaced in the UI via streamed `last_push` state, and never blocks the run). Generated source code is committed separately (`git add -A`) at every green rebuild and after each quality/security/dependency fix round, so the pushed branch always carries the code, not just the artifacts. Every LLM node appends a ledger entry with its token usage; a fresh run always re-enters at `intake`, abandoning any interrupt a previous run left open.

---

## Headless runner (full pipeline, no UI)

Run the entire graph programmatically for a repo/branch — spec and plan auto-approve (the only
interrupts left in the graph), clarifying questions are disallowed (drafts are told to make and
record assumptions), and any failure ENDs the run with `run_failure` in the JSON report:

```bash
cd agent && uv run python run_headless.py <owner> <repo> <branch> --requirements-file req.md
```

Needs `GITHUB_TOKEN` (Copilot) and `E2E_GITHUB_TOKEN` (clone + push — must have push scope) in the
root `.env`. Writes a JSON summary to `agent/agent-work/headless-<thread>.json`; exit code 0 only
when the exit stage approved. Expect hours of wall time and real Copilot spend for a full run.

---

## E2E test mode (bypassing GitHub OAuth + MFA)

Automated browser tests cannot complete the real GitHub sign-in (OAuth + MFA). For local end-to-end testing only, set both:

```bash
AIDW_E2E_MODE=1            # activates the bypass; hard-refused when NODE_ENV=production
E2E_GITHUB_TOKEN=<PAT>     # classic PAT with `repo` read on the target repos
```

With E2E mode active: the middleware ([src/proxy.ts](src/proxy.ts)) stops enforcing sign-in, GitHub API routes fall back to the PAT ([src/lib/e2e.ts](src/lib/e2e.ts)), session provisioning forwards the PAT as the sandbox clone credential under the synthetic identity `e2e-user`, and a warning is logged at startup. Playwright can then drive `/select` → `/workflow/...` directly. Both variables default to off; the guard is conjunctive with the production check in every consumer.

---

## Migration: single `ai-dev-workflow` branch per repo

Sessions used to work on a per-session branch, `ai-dev-workflow/<selected-branch>-<session
prefix>` -- one branch per repo+branch+user, so two sessions could never collide. As of this
migration every session on a repo shares a single `ai-dev-workflow` branch instead; the branch
picker in the UI is now a PR-target selector (where the pipeline's work eventually merges to),
not part of a session's identity (`deriveThreadId` in
[src/lib/workflow-thread.ts](src/lib/workflow-thread.ts) keys threads by repo+user only).

Legacy `ai-dev-workflow/<branch>-<prefix>` branches, and the sandbox containers/volumes that were
attached to them, become invisible to the tool the moment this migration lands: nothing reads,
writes, or reaps them anymore. Clean up orphaned pre-migration state by hand:

```bash
# Local Docker: sandbox containers and their workspace volumes (both pre- and post-migration
# sessions share this naming, so check `docker inspect <name>` before removing one you're unsure
# about -- REPO_BRANCH in its env tells you which scheme provisioned it)
docker ps -a --filter name=ai-dev-workflow-sandbox-
docker volume ls --filter name=aidw-ws-

# GitHub: the old per-session branches themselves -- this naming pattern is unique to the
# pre-migration scheme, so everything matching it is safe to delete once merged/abandoned
git ls-remote --heads origin 'ai-dev-workflow/*'
```

---

## Keeping this diagram current

The diagram is generated by hand but guarded automatically. [.claude/hooks/graph-diagram-check.mjs](.claude/hooks/graph-diagram-check.mjs) hashes every file that defines the graph — `graph.py`, the node-cluster modules, `agent/src/gates/`, and `agent/src/prompts/` — and stamps that hash into this README. Two hooks in [.claude/settings.json](.claude/settings.json) run it:

- **PostToolUse** (after any edit) — injects a note telling Claude the diagram is stale.
- **Stop** (before the turn ends) — blocks the turn while the diagram is still stale, so it does not get forgotten.

After updating the diagram, re-stamp it:

```bash
node .claude/hooks/graph-diagram-check.mjs --stamp
```

<!-- graph-source-sha256: aa6ed5383b90fafdbcd39ded44b39ee1ace1e5729fcf02a4604ea351611b9832 -->
