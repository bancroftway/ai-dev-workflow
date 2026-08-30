# Configuration catalog

Every variable the code reads. Each lives in the config vault as a secret named with dashes
instead of underscores (`AUTH_SECRET` -> secret `AUTH-SECRET`); both processes inject them as
env vars at boot. See the README section "Configuration from Key Vault" for the mechanism,
seeding loop and role grant. Values shown are the code's own defaults — a variable you don't set
behaves exactly as shown. Without a vault, the same lines can go straight into the repo-root
`.env` instead.

```bash
# ── Secrets / identity (required for a real deployment) ─────────────────────────────────────────

# NextAuth JWT encryption secret (generate: `openssl rand -base64 32`).
AUTH_SECRET=

# GitHub OAuth app for the LINKED GitHub account (Auth.js convention names).
AUTH_GITHUB_ID=
AUTH_GITHUB_SECRET=

# Entra ID app registration — ONE app, read by both frontend (sign-in, OBO assertion) and agent
# (OnBehalfOfCredential for per-user Key Vault reads). See infra/README.md for the portal steps
# (redirect URI, access_as_user scope + admin consent, client secret).
AZURE_TENANT_ID=
AIDW_AGENT_APP_ID=
AIDW_AGENT_CLIENT_SECRET=

# Shared secret the frontend sends on every agent API call; the agent rejects requests without it
# when set. Same value on both sides (one file locally, two app configs in Azure).
AIDW_AGENT_SHARED_SECRET=

# LLM credentials — the resolved provider picks which one is used. org_settings (DB, set via the
# organization settings page) overrides AGENT_PROVIDER; these env values are the fallback.
ANTHROPIC_API_KEY=
CLAUDE_CODE_OAUTH_TOKEN=          # alternative Claude billing: `claude setup-token` subscription token
GITHUB_TOKEN=                     # Copilot provider credential (also generic GitHub API fallback)

# ── Frontend ↔ agent wiring ─────────────────────────────────────────────────────────────────────

AGENT_URL=http://localhost:8123   # where the frontend reaches the agent API
PORT=8123                         # agent API listen port

# ── E2E / headless test mode ────────────────────────────────────────────────────────────────────

# 1 = frontend bypasses OAuth/MFA entirely (non-prod only). A run provisioned this way carries no
# Entra assertion, so Key Vault fetch is skipped and the auth gate is INERT — never leave this on
# when verifying auth behavior.
AIDW_E2E_MODE=0
# PAT with clone+push scope on target repos; used by headless runs and the E2E-bypass frontend in
# place of a signed-in user's token.
E2E_GITHUB_TOKEN=

# ── Provider / sandbox ──────────────────────────────────────────────────────────────────────────

AGENT_PROVIDER=claude             # claude | copilot — env fallback; DB org_settings wins when set
SANDBOX_PROVIDER=local            # local (Docker) | aci (Azure Container Instances)
AIDW_SANDBOX_IDLE_TIMEOUT=1800    # seconds before an idle sandbox container is reaped; headless
                                  # runs export 86400 — export it yourself for long-paused UI runs
AIDW_SANDBOX_PROVISION_RETRY_ATTEMPTS=2
AIDW_TOOLCHAIN_LOG=agent/agent-work/toolchain.jsonl  # host-side "what did bootstrap install" log

# Azure ACI sandbox provider only (SANDBOX_PROVIDER=aci):
AZURE_RESOURCE_GROUP=
AZURE_ACI_LOCATION=
AZURE_ACI_IDENTITY=
AZURE_ACI_SANDBOX_IMAGE=
AZURE_ACI_REGISTRY_SERVER=
AZURE_ACI_REGISTRY_USERNAME=
AZURE_ACI_REGISTRY_PASSWORD=
AZURE_ACI_VNET_NAME=
AZURE_ACI_SUBNET_NAME=
AIDW_CACHE_SHARE=                 # Azure Files share for package caches; OFF unless set (SMB can
                                  # be slower than re-downloading — enable after measuring)
AIDW_CACHE_STORAGE_ACCOUNT=
AIDW_CACHE_STORAGE_KEY=

# ── Database ────────────────────────────────────────────────────────────────────────────────────

# When AZURE_SQL_SERVER is unset, the agent falls back to local SQL Server (trusted connection).
AZURE_SQL_SERVER=
AZURE_SQL_DATABASE=
AIDW_SQL_LOCAL_SERVER=localhost
AIDW_SQL_LOCAL_DATABASE=Ai-Dev-Workflow

# ── Auth-enforcement gate / org vault ───────────────────────────────────────────────────────────

# Kill-switch for the whole auth-enforcement chain (prompt segments + e2e probe + exit backstop).
# Default ON. WARNING: an EMPTY value ("AIDW_AUTH_GATE=") counts as OFF — delete the line instead.
AIDW_AUTH_GATE=1
AZURE_ORG_VAULT_URI=              # org-level Key Vault holding the org LLM credential pointer

# ── Headless runner (set by run_headless.py itself — listed for completeness, don't set) ────────

#AIDW_HEADLESS=1                  # marks the in-process run as headless (auto-approve gates)
#AIDW_RESUME=1                    # set by --thread resume; skips approved stages (metrics-exit
                                  # always re-judges regardless)
AIDW_LOG_LEVEL=INFO

# ── Deterministic gate thresholds (all optional; defaults shown) ────────────────────────────────

MIN_COVERAGE_PERCENT=95.0         # line+branch coverage floor at minimal-code-to-green
MAX_DUPLICATION_PERCENT=3.0       # jscpd duplication ceiling (metrics regression gate)
QUALITY_MAX_DUPLICATION_PERCENT=3.0  # same ceiling as seen by the remediation quality scan
# Near-duplicate test detection (ac_coverage_gate): two tests for one criterion count as ONE when
# their bodies are >= this similar AND they assert the same normalised targets. Raise toward 1.0
# to only catch verbatim copies; don't lower it — small tests share scaffolding legitimately.
MAX_TEST_BODY_SIMILARITY=0.92
MIN_DISTINCT_ASSERTIONS_PER_AC=2  # distinct assertion targets required once an AC has enough tests
MIN_TESTS_BEFORE_ASSERTION_CHECK=3  # assertion-diversity check only kicks in at this many tests
MIN_NON_E2E_TESTS_PER_AC=2        # sub-browser (unit/integration) tests per criterion at mctg
MIN_NON_E2E_TESTS_PER_AC_RED=0    # same check during the TDD-red phase (stubs allowed)
LIZARD_MAX_CCN=20                 # cyclomatic complexity: finding threshold
LIZARD_HIGH_CCN=25                # cyclomatic complexity: high-severity threshold
SECURITY_SEVERITY_FLOOR=medium    # semgrep/OSV findings below this never gate
HEALTH_MIN_SECURITY_COVERAGE=1.0  # below this fraction of security tools completing, score *= sqrt(fraction)
DOC_COVERAGE_MIN_PERCENT=50.0     # public-API doc-comment coverage floor
HEALTH_REGRESSION_TOLERANCE=2.0   # health-score points a run may drop vs baseline before blocking
METRIC_REGRESSION_TOLERANCE=1.0   # per-metric regression tolerance (coverage etc.)
LIGHTHOUSE_PERF_MIN=0             # lighthouse performance floor for UI repos (0 = advisory only)
LIGHTHOUSE_A11Y_MIN=90            # lighthouse accessibility floor for UI repos
LIGHTHOUSE_BLOCKING_AUDITS=color-contrast  # audit ids that block e2e on their own, whatever the score (comma-separated; empty disables)
REPO_SCAN_CHURN_WINDOW_DAYS=365   # git-churn window for the maintainability subscore
REPO_SCAN_COVERAGE_TIMEOUT_SECONDS=600
AIDW_SEMGREP_RULES_DIR=/opt/aidw/semgrep-rules  # baked into the sandbox image
AIDW_OSV_DB_DIR=/opt/aidw/osv-db                # baked into the sandbox image

# ── Lap caps, retries, timeouts (all optional; defaults shown) ──────────────────────────────────

TECH_STACK_MAX_CLARIFICATION_CYCLES=2
SPEC_MAX_CLARIFICATION_CYCLES=3
PLAN_MAX_CLARIFICATION_CYCLES=3
AC_TO_TESTS_MAX_CLARIFICATION_CYCLES=3
MINIMAL_CODE_TO_GREEN_MAX_CLARIFICATION_CYCLES=3
ADVERSARIAL_AUDIT_MAX_CLARIFICATION_CYCLES=2
EXIT_MAX_CLARIFICATION_CYCLES=2
E2E_MAX_FIX_CYCLES=8              # e2e fix-loop laps before the run escalates
E2E_APP_READY_TIMEOUT_SECONDS=120 # app boot wait before the suite runs
E2E_SUITE_TIMEOUT_SECONDS=1200    # playwright suite ceiling
TEST_HARDENING_MAX_FIX_CYCLES=4   # stable-regression fix laps before the gate ends the run
TEST_HARDENING_TOTAL_ATTEMPTS=3   # suite runs (Nx) for flake triage
AIDW_VERIFY_STALL_LAPS=2          # identical verify feedback this many laps in a row = stall
CLI_AGENT_TURN_TIMEOUT_SECONDS=2400  # one Claude CLI turn's wall ceiling inside the sandbox
AIDW_LLM_INFRA_RETRY_ATTEMPTS=3
AIDW_LLM_INFRA_RETRY_BACKOFF_SECONDS=5,20,60
EVAL_ATTEMPTS=3                   # skill-eval harness only
EVAL_TIMEOUT_SECONDS=900

# ── Frontend metrics-bar grade bands (letter-grade cutoffs, comma-separated) ────────────────────

METRIC_CCN_GRADES=5,10,15,20      # mean cyclomatic complexity, ascending (lower is better)
METRIC_COVERAGE_GRADES=80,70,50,30 # line coverage %, descending (higher is better)
METRIC_DUP_GRADES=3,5,10,20       # duplicated-code %, ascending (lower is better)
METRIC_LH_PERF_GRADES=90,75,60,40 # lighthouse performance score, descending
METRIC_A11Y_GRADES=95,90,80,60    # lighthouse accessibility score, descending

# ── Telemetry (optional) ────────────────────────────────────────────────────────────────────────

OTEL_TRACES_EXPORTER=             # e.g. otlp; empty = tracing off
OTEL_EXPORTER_OTLP_ENDPOINT=
```
