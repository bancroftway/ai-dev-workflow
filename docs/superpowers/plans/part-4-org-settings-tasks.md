# Part 4 — Org settings: task breakdown

Spec (binding authority): C:\Users\jblis\.claude\plans\inside-the-staging-container-sunny-tome.md
— Part 4's own section, plus the "Rollout and sequencing" section's reasoning for why Part 4 ships
second (right after Part 1, before Parts 3/2). This file argues from that Spec but is grounded in
what Part 1 *actually built* on `feature/claude-support` (not the Spec's description of code that
didn't exist yet when it was written) — several real divergences were found while drafting this
and are recorded as Rulings below, not silently absorbed.

## What Part 1 actually built that this plan builds on (verified against real code, 2026-08-22)

- `agent/src/chat_model.py`: `PROVIDER = os.environ.get("AGENT_PROVIDER", "copilot")` is a
  **module-load-time** constant. The `if PROVIDER == "copilot": ... elif ... claude ...` block
  **re-exports bound function objects** (`get_chat_model_for_thread`, `close_session`,
  `close_thread_session`, `forget_thread_sessions`, `get_session_id`, `read_skill_invocations`,
  `secret_env_names`) chosen once, at import time.
- Real call-site inventory (grepped, not estimated): **~17 sites** compare `chat_model.PROVIDER`
  to `"claude"`/`"copilot"` directly (`graph.py` ×4, `e2e_nodes.py`, `metrics_nodes.py`,
  `rebuild.py`, `preflight_nodes.py` ×2, `test_hardening_nodes.py` ×2, `stack_runner.py`,
  `sessions_api.py` ×2, `run_headless.py` ×2, `sandbox/local_docker.py` ×2,
  `sandbox/azure_aci.py`). **~10 more sites** do `from .chat_model import get_chat_model_for_thread`
  (or a sibling name) as a bare-name import, then call it directly, across `graph.py`,
  `e2e_nodes.py`, `metrics_nodes.py`, `rebuild.py`, `preflight_nodes.py`, `stack_runner.py`,
  `test_hardening_nodes.py`, `gates/skill_gate.py`, `sandbox/registry.py`, `telemetry.py`.
- `model_config.get_model_name(stage, role, provider)` (Task 10) already takes `provider` as an
  **explicit string parameter** — its own comment at line ~57 literally says "Part 4 (runtime-
  configurable provider) doesn't exist yet." This plan does NOT need to touch `model_config.py`'s
  signature — only what every caller passes as the third argument changes.
- `agent/src/keyvault.py` + `agent/db/migrations/0002_create_repo_vaults.sql`: the existing
  secrets pattern is **per-user, per-repo, OBO-delegated** (`dbo.repo_vaults` keyed on
  `(owner, repo, user_login)`; a fresh Entra assertion from the frontend, forwarded through
  `ProvisionRequest.entra_assertion`, is exchanged via `OnBehalfOfCredential` for a vault-scoped
  token carrying the *user's* identity; the agent itself holds **no standing vault access** — this
  is stated as a hard enterprise constraint in the module's own docstring).
- `src/app/(boxed)/settings/[owner]/[repo]/page.tsx` + `src/app/api/repos/vault/route.ts`: the
  real, existing UI/BFF/backend pattern for a settings page — client component, raw Tailwind
  (no component library), a `SaveState` discriminated union (`idle`/`saving`/`saved`/`error`),
  `getServerAuthToken()` for the signed-in user's session + Entra token, `agentFetch()` as the
  BFF→agent HTTP client, `hasRepoAccess()` for authorization. The existing page's own copy is
  explicit that "the service itself gets no standing access" — this is a *deliberate, stated*
  design principle of the existing settings surface, not an accident.
- `infra/main.bicep` (Task 11) already has `agentProvider` (`@allowed(['copilot','claude'])`,
  default `'copilot'`) and `anthropicApiKey` (`@secure()`) params, wired as Container App
  secrets/env — this is the **deploy-time** fallback Part 1 shipped. Part 4 adds a
  **runtime-configurable** layer on top; it does not replace this, since a fresh deployment with
  no org setting saved yet needs *some* default.
- `agent/src/graph.py:159`: `class GraphState(TypedDict)`. **`thread_id` is NOT a `GraphState`
  field** — nodes receive `(state: GraphState, config: RunnableConfig)` and read
  `config["configurable"]["thread_id"]`. This matters for Ruling 2 below.

## Ruling 1 — the org-wide credential needs the agent's own standing Key Vault access, not OBO

The Spec's text ("stored the same way app secrets already are — through the existing Entra
on-behalf-of Key Vault fetch") does not actually fit here, and building it as written would not
work correctly. `keyvault.py`'s OBO pattern answers "can fetch secrets AS THE SIGNED-IN USER,
scoped to what that user's own Azure RBAC grants" — the right model for a *user's own* per-repo
vault, where "the agent has no standing access" is precisely the safety property wanted. An
org-wide coding-agent credential (the Anthropic key or Copilot PAT the *whole fleet* uses) has no
natural "as which user" framing: whoever is provisioning a session to work a ticket is very
unlikely to be the admin who configured the credential, and has no reason to hold Azure RBAC on
whatever vault holds it. Forcing OBO here would mean every session provision either needs the
*admin's* still-fresh assertion (impossible — assertions expire in ~1h and provisioning happens
whenever any user files a ticket, not when the admin is nearby) or silently falls back to
something else, defeating the point of centralizing it in a vault at all.

**Decision**: a small, dedicated Key Vault (or a clearly-namespaced secret in an existing one —
Task 2 below decides which, after checking what's actually provisioned in `infra/main.bicep`
today) that the agent's own managed identity has **standing, narrowly-scoped** read access to —
`Key Vault Secrets User`, RBAC-scoped to *only* this one vault/secret, nothing else. This is a
deliberate, narrow, documented exception to "no standing vault access," not a reversal of that
principle — the principle exists to stop one identity reaching into *every team's* vault; a single
fleet-wide secret with no natural per-user owner is exactly the case that principle doesn't cover.
Cost if wrong: an over-scoped grant would be a real security regression — Task 2's own
verification step must confirm the RBAC scope is genuinely narrow (this one vault only), not
assumed from the bicep template compiling.

## Ruling 2 — a session's provider must be pinned at provisioning, not re-read live on every call

The Spec says "a provider change never affects an in-flight run" but doesn't mechanically solve
it, and Part 1's ~17 real call sites all currently read the (today static) `chat_model.PROVIDER`
fresh, every time. Once the org setting is live-editable, reading it fresh from every graph node
would violate the Spec's own stated design the instant an admin changes it mid-run.

**Decision**: `GraphState` (verified above: a `TypedDict`, already the mechanism `stages`/
`used_ids`/etc. rely on to survive a resumed run — Task 4 below must confirm this checkpointing
behavior against real intake/resume code before relying on it further, not just assume it because
other fields do it) gains a `provider: Literal["copilot", "claude"]` field, populated **once**, at
intake, from `chat_model.get_provider()` — the one live read of the current org setting for this
entire run. Every one of the ~17 in-graph-node call sites reads `state["provider"]` instead of
`chat_model.PROVIDER`. Call sites that are themselves *provisioning a new session*
(`sessions_api.py`'s `provision_session`, `run_headless.py`'s startup, the sandbox provisioning
lazy imports in `local_docker.py`/`azure_aci.py`) correctly keep calling `chat_model.get_provider()`
live — provisioning is exactly the one moment a run is *allowed* to pick up the current setting.

## Ruling 3 — credential validation timing (Spec's own flagged open gap, resolved here)

The Spec explicitly left this undecided ("adds latency/cost to every session start, cached with a
TTL, or a periodic background job?"). Resolved by matching the *existing* per-repo settings page's
own precedent exactly, rather than inventing new machinery: `RepoSettingsPage`'s "Save & test
access" button test-reads the vault at save time, so a successful save is itself proof the grant
works. Org settings do the same — saving a credential test-fetches it immediately (same
request/response cycle, no separate validation endpoint). Session provisioning does not add a
*second* validation round-trip either: it already has to fetch the credential value to use it, so
"the fetch succeeds" doubles as validation with zero added latency. No TTL cache, no background
job, no new mechanism — this was already the right shape once framed as "fetch failure IS the
validation signal," matching this codebase's existing fail-fast-with-the-provider's-own-error
pattern (`sessions_api.py`'s vault-fetch-before-provision already works this way for the
per-repo case).

## Global Constraints (apply to every task)

- Repo root: `d:\Projects\bancroftway\ai-dev-workflow`. Backend work under `agent/`, frontend
  under `src/`.
- Python 3.12, venv at `agent/.venv` (Windows). Run tools as `agent/.venv/Scripts/python.exe`.
- Frontend: Next.js App Router, TypeScript, raw Tailwind utility classes (no component library —
  match `RepoSettingsPage`'s existing visual language: `rounded-lg`/`rounded-md` borders,
  `neutral-900` primary buttons, `green-700` success text, `red-50`/`red-200` error boxes).
- Match each codebase's existing comment/docstring style exactly. Dense why-comments only where a
  decision needs justifying — this plan's own Rulings above are exactly the kind of thing worth
  citing in-code, not re-derived silently by whoever implements each task.
- Never commit secrets. Never touch `.env`.
- This is a live production pipeline — anything a task doesn't explicitly change must keep working
  exactly as it does today. In particular: `AGENT_PROVIDER` env var / `agentProvider` bicep param
  must keep working as the **fallback default** for a fresh deployment with no org setting saved
  yet — Part 4 adds a layer on top, it does not remove the one Part 1 shipped.
  `GraphState` gaining `provider` must not affect any run mid-flight when this ships — Task 4/5's
  own migration note must cover what an *already-checkpointed* run (started before this lands)
  does when resumed after (see Task 4's brief).
- No subagents: implementers never dispatch their own subagents or reviewers.
- Every new/rewritten Python module needs a `_demo()` self-check following the exact package
  re-dispatch `__main__` pattern already established on this branch (`chat_model.py`,
  `claude_chat_model.py`, etc. — copy from one of them).
- After every backend task: `agent/.venv/Scripts/python.exe -m py_compile <every touched file>`,
  then the dual-provider whole-app import check:
  ```
  cd agent
  .venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'.'); from src import graph; import main"
  AGENT_PROVIDER=claude .venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'.'); from src import graph; import main"
  ```
- After every frontend task: confirm the Next.js app still builds/typechecks (`npm run build` or
  the project's real equivalent — check `package.json`'s actual script names before assuming).

---

## Task 1: DB migration + org_settings store module

**Files:** new `agent/db/migrations/0003_create_org_settings.sql`, new
`agent/src/org_settings.py`. Depends on nothing above.

Read `agent/src/session_store.py` (specifically `_get_pool()`) and `agent/src/keyvault.py`
(specifically `get_vault_uri`/`set_vault_uri`'s MERGE-based upsert shape) first — copy their real
patterns exactly, don't reinvent.

**Migration** (mirror `0002_create_repo_vaults.sql`'s header-comment style, citing the real reason
for each design choice): a table holding exactly one conceptual row per deployment (this pipeline
has no multi-tenant/org-id concept today — one deployment is one org, matching
`infra/main.bicep`'s own one-Container-App-per-deployment shape). Use a fixed, application-enforced
single-row convention (e.g. a `CHECK (id = 1)` constraint on an `id INT NOT NULL PRIMARY KEY`
column, matching how a genuinely singleton table is usually pinned in SQL Server — verify this is
idiomatic before committing to it, a simpler alternative is fine if you find one) — columns:
`provider NVARCHAR(16) NOT NULL` (`'copilot'`/`'claude'`), `credential_secret_name NVARCHAR(255)
NULL` (the Key Vault secret name Task 2's vault holds the actual value under — never the secret
value itself), `updated_at DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()`,
`updated_by NVARCHAR(255) NULL` (the admin's GitHub or Entra login, for an audit trail — check
what identity string is actually available at the call site before picking one).

**`org_settings.py`**: `async def get_org_settings() -> OrgSettings | None` (a small
`@dataclass(frozen=True)` — `provider: str`, `credential_secret_name: str | None`, `updated_at`,
`updated_by`), `async def set_org_settings(provider: str, credential_secret_name: str | None,
updated_by: str) -> None` (MERGE upsert against the single fixed row id, mirroring
`set_vault_uri`'s exact shape). No caching here — Task 3 owns the TTL cache, this module is the
plain DB access layer underneath it.

Self-check: `_demo()` can only exercise pure logic (no live DB in this environment, matching every
other DB-touching module's own self-check limitation on this branch) — assert the dataclass
round-trips its fields correctly; note in a comment that the real MERGE/SELECT statements are
verified by Part 4's own final verification task, against a real DB, the same way Part 1's Task 12
verified against a real container.

---

## Task 2: Org-wide credential vault (standing access) + Bicep wiring

**Files:** `infra/main.bicep`, new `agent/src/org_credential_vault.py`. Depends on Task 1 (for
the `credential_secret_name` column shape) and Ruling 1 above.

Read `infra/main.bicep` in full first — confirm exactly what Key Vault resources (if any) already
exist in this template before deciding whether to add a new vault resource or a namespaced secret
in an existing one. If a vault already exists that the agent's managed identity could reasonably
be scoped into without widening its existing access to anything else, prefer reusing it (one fewer
resource); if not, add a new, minimal Key Vault resource dedicated to this one purpose.

Add (whichever the read above determines): the vault resource (if new), an RBAC role assignment
granting the agent's Container App managed identity `Key Vault Secrets User`, **scoped to this one
vault only** (verify the scope string in the generated ARM JSON after `az bicep build`, don't just
trust the bicep source reads right — this is exactly the kind of thing worth a real check per
Ruling 1's own stated cost-if-wrong).

**`org_credential_vault.py`**: `async def get_org_credential(secret_name: str) -> str` (uses the
agent's own **standing** identity — `azure.identity.aio.DefaultAzureCredential` or
`ManagedIdentityCredential`, whichever this codebase's existing Azure auth code already uses
elsewhere for standing access if such a precedent exists; check `agent/src/` for one before
picking) — NOT `OnBehalfOfCredential`, no `entra_assertion` parameter, this is the deliberate
divergence from `keyvault.py`'s per-repo pattern per Ruling 1. `async def set_org_credential(value:
str) -> str` (writes the secret, returns the generated secret name — e.g. a fixed well-known name
like `org-provider-credential`, versioned by Key Vault's own secret versioning rather than this
codebase inventing its own). Raise the same `VaultAccessError`-shaped exception `keyvault.py`
already defines (import and reuse it, don't duplicate the exception class) on any Azure SDK
failure, carrying the real Azure error detail — matches this codebase's existing "fail fast with
the provider's own error" convention.

Self-check: pure-logic only (secret-name generation, if any transformation is involved) — real
vault I/O verified in Part 4's final verification task.

---

## Task 3: `chat_model.py` dispatch rework

**Files:** `agent/src/chat_model.py`. Depends on Tasks 1-2. This is the deepest, most
delicate task in this plan — read it as carefully as Part 1's Task 6 (the readiness-check
rewrite that took two real fix rounds to get right).

Read the CURRENT `agent/src/chat_model.py` in full (reproduced in this plan's own "What Part 1
actually built" section above, but read the real file — it may have drifted).

Replace the import-time `if PROVIDER == "copilot": from .copilot_chat_model import (...)` block
with:
- `async def get_provider() -> str`: reads `org_settings.get_org_settings()`, cached with a short
  TTL (pick a concrete number — 30-60 seconds is reasonable, matching "an admin's change should
  take effect soon, not instantly, and shouldn't hit the DB on every single call" — state your
  reasoning in a comment) via a module-level `(value, fetched_at)` tuple, not a decorator library
  this codebase doesn't already depend on. Falls back to `os.environ.get("AGENT_PROVIDER",
  "copilot")` if `get_org_settings()` returns `None` (fresh deployment, nothing saved yet — this is
  the Global Constraints' explicitly-required fallback-default behavior).
- Every currently-re-exported name (`get_chat_model_for_thread`, `close_session`,
  `close_thread_session`, `forget_thread_sessions`, `get_session_id`, `read_skill_invocations`,
  `secret_env_names`) becomes a **real function defined in `chat_model.py` itself** — not a bound
  alias chosen once at import time — that calls `await get_provider()` (or, for the sync
  `get_session_id`, whatever sync-safe equivalent makes sense — check its real current signature,
  it may not be async) and dispatches to `claude_chat_model.<name>(...)` or
  `copilot_chat_model.<name>(...)` accordingly, on every call. This is the actual fix for the
  staleness problem the Spec's text didn't fully anticipate (see "What Part 1 actually built"
  above) — a bare-name importer (`from .chat_model import get_chat_model_for_thread`) now gets a
  function that re-resolves the provider every time it's called, not a stale binding from process
  startup.
- Both `claude_chat_model.py` and `copilot_chat_model.py` must import cleanly regardless of which
  provider is active (they both already do, per Part 1) — the module no longer picks one to import
  and skip the other; both get imported unconditionally so either can be dispatched to at call
  time. Confirm this doesn't introduce a real import-time cost/side-effect problem (read both
  modules' top-level code for anything expensive or side-effecting at import time before assuming
  this is free).
- `ainvoke_structured` (from `structured_output.py`) is unaffected — it was already
  provider-agnostic, not part of this dispatch.

Self-check: `_demo()` needs real rework too — it can no longer assert a single `PROVIDER` value
(there isn't one anymore); instead assert that calling `get_provider()` returns the env-var
fallback when no org settings exist (mock/stub `org_settings.get_org_settings` to return `None`
for this pure-logic path), and that every re-exported name is callable and, when invoked with a
stubbed provider value, dispatches to the right underlying module (check via a distinguishing
return value or a monkeypatched marker — your call on the cleanest way, but the test must actually
prove dispatch happens per-call, not just that the function exists).

---

## Task 4: `GraphState` gains a pinned `provider` field

**Files:** `agent/src/graph.py` (the `GraphState` TypedDict and intake node), possibly
`agent/src/preflight_nodes.py` if intake logic lives partly there — check both. Depends on Task 3.

Read `agent/src/graph.py`'s real `GraphState` definition (line ~159) and whichever node runs
**first** in the graph (intake) in full before changing anything — confirm your understanding of
where a brand-new run's state gets its first values, and how a **resumed** run (one whose
checkpoint already exists from before this field existed) behaves when it's read. This second
point is the one genuinely tricky migration question in this task: an in-flight run started before
this ships has a checkpointed state dict with no `provider` key at all. Every downstream read of
`state["provider"]` needs a safe fallback for that case (e.g. `state.get("provider") or
await chat_model.get_provider()` at each read site is NOT what Ruling 2 wants long-term, but is the
correct one-time bridge for a run that started before this field existed — decide and document
this explicitly, don't silently pick one behavior).

Add `provider: Literal["copilot", "claude"]` to `GraphState`. Populate it **once**, in the intake
node, via `await chat_model.get_provider()` — the one live read this whole design permits per run.
Confirm (empirically, by tracing a real resume path in the code, not by assuming) that a custom
`GraphState` field set once at intake genuinely survives a checkpointed resume the same way
`stages`/`used_ids` already do — this plan's Ruling 2 depends on that being true; if it turns out
NOT to be true for a field that isn't touched again after intake, escalate rather than proceeding
on an unverified assumption (this is exactly the kind of thing worth a NEEDS_CONTEXT report if the
real checkpointing behavior doesn't match what this brief assumes).

Self-check: N/A for this task specifically (state-shape changes aren't independently
self-checkable without a live graph run) — covered by Task 6's final verification.

---

## Task 5: Convert the ~17 read sites + ~10 bare-name-import sites

**Files:** `agent/src/graph.py`, `agent/src/e2e_nodes.py`, `agent/src/metrics_nodes.py`,
`agent/src/rebuild.py`, `agent/src/preflight_nodes.py`, `agent/src/test_hardening_nodes.py`,
`agent/src/stack_runner.py`, `agent/src/gates/skill_gate.py`, `agent/src/sandbox/registry.py`,
`agent/src/telemetry.py`. Depends on Tasks 3-4.

Grep for `chat_model.PROVIDER` and `from .chat_model import` / `from ..chat_model import` across
`agent/src` yourself first — this plan's own inventory (in "What Part 1 actually built" above) is
a snapshot from 2026-08-22 and may have drifted by the time you implement this; treat it as a
starting list to verify, not a final one.

For every call site **inside a graph node** (has access to `state`): replace `chat_model.PROVIDER`
with `state["provider"]` (applying Task 4's resume-safety fallback if that site could plausibly run
against a pre-migration checkpoint). For every bare-name import of a dispatched function
(`get_chat_model_for_thread`, etc.) inside a graph node, no code change is needed beyond what Task
3 already did — the imported name is now a real per-call-dispatching function, it just needs the
`provider`/model-name argument threaded from `state["provider"]` wherever the call passes a
`model_name=model_config.get_model_name(..., chat_model.PROVIDER)` argument (this is most of the 17
sites — the third argument changes from `chat_model.PROVIDER` to `state["provider"]`, nothing else
in that call changes).

For every call site **outside a graph node** — `sessions_api.py`'s `provision_session`,
`run_headless.py`'s startup, `sandbox/local_docker.py`/`sandbox/azure_aci.py`'s lazy `from
..chat_model import PROVIDER` — these are exactly the "provisioning a new session" moments Ruling 2
says should keep reading live: change `from ..chat_model import PROVIDER` (a now-broken bare-name
import, since `PROVIDER` is no longer a module-level constant) to `from ..chat_model import
get_provider` and `await get_provider()` at the call site instead.

`gates/skill_gate.py`, `sandbox/registry.py`, `telemetry.py`'s bare-name imports of
`forget_thread_sessions`/`get_session_id`/`read_skill_invocations` need no behavior change beyond
confirming they still import cleanly against Task 3's rework (these functions dispatch internally
now; callers outside graph nodes that don't need a *specific* provider's behavior, just "whichever
one is active for this session," are fine calling the dispatching function directly without
threading a provider argument through — verify this is true for each of these three specific call
sites, since "which session's provider" matters for these particular functions in a way it might
not for a stateless one).

Self-check: no new self-checks — covered by re-running every already-existing self-check this task
touches, plus Task 6's final verification.

---

## Task 6: Backend org-settings API (sessions_api.py)

**Files:** `agent/src/sessions_api.py` (or a new sibling router module if this file is getting
large — check its current size before deciding), `agent/main.py` (route registration, mirroring
however `vault-config` is already wired). Depends on Tasks 1-2.

Read `sessions_api.py`'s existing `vault-config` GET/PUT handlers in full first — mirror their
exact shape (request/response Pydantic models, error handling, status codes) for the new
endpoints, don't invent a different style for the same kind of settings CRUD in the same file.

`GET /org-settings`: returns `{provider, credential_configured: bool, updated_at, updated_by}` —
**never** the credential value itself (matches the existing per-repo settings page's write-only
convention, and Part 4's Spec's own explicit "credential is write-only once saved" gap resolution
— state that resolution here as the design, don't leave it implicit).

`PUT /org-settings`: body `{provider, credential: str | None}` (omitted/`null` credential means
"keep whatever's already saved," matching the masked-dots-plus-Update-button UI pattern the Spec's
wireframe describes). On a provided credential: call `org_credential_vault.set_org_credential`,
then **immediately test-fetch it back** (Ruling 3 — validate at save time, not a separate
endpoint) using the same real mechanism a session provision would use (whichever real CLI/API call
actually proves the credential works — check what's realistic to test synchronously in an HTTP
request/response cycle without waiting on a full sandbox provision; if a lightweight check isn't
available, say so in your report and propose the smallest real check that is, rather than skipping
validation silently). On success, `org_settings.set_org_settings(...)`. On failure, return the
provider's own real error detail (matching `vault-config`'s existing "surface the real error, not
a generic one" convention) and do **not** save the new setting — the previous org setting/provider
stays live at `chat_model.get_provider()` until a validated save succeeds.

Self-check: N/A (needs a live DB + live credential to test meaningfully) — covered by Task 9.

---

## Task 7: Frontend Organization Settings page + BFF route

**Files:** new `src/app/(boxed)/settings/organization/page.tsx`, new
`src/app/api/settings/organization/route.ts`. Depends on Task 6.

Read `src/app/(boxed)/settings/[owner]/[repo]/page.tsx` and `src/app/api/repos/vault/route.ts` in
full first (already reproduced in overview above, but read the real files) — this page is a
sibling in the same `(boxed)/settings/` route family; match its component shape (`SaveState`
union, raw Tailwind classes, loading/saved/error rendering) exactly rather than introducing a new
visual style for the same kind of settings surface.

Page: provider radio choice (Copilot / Claude Code), a credential input that shows masked dots
once configured (never the real value — `GET /org-settings`'s `credential_configured: bool` drives
whether to show "configured, click to change" vs. an empty input), Save button that calls `PUT
/api/settings/organization`. Route handler: same BFF shape as `vault/route.ts`
(`getServerAuthToken()`, forward to the agent via `agentFetch()`) but **no `entra_assertion`
forwarding** (Ruling 1 — this isn't an OBO-backed endpoint) — check with whoever owns
authorization conventions (or find the real existing pattern in this codebase, e.g. how
`hasRepoAccess` is used) what gates who may view/edit **org**-level settings specifically, since
`hasRepoAccess` itself is repo-scoped and doesn't obviously apply here — this needs its own
verified answer, not an assumption carried over from the per-repo page.

Self-check: N/A (frontend component) — confirm the app still builds/typechecks per Global
Constraints.

---

## Task 8: Not-configured banner

**Files:** a new shared component (check `src/components/` for where a banner like this belongs),
wired into wherever a session/run would otherwise start (check the real current New
Ticket/session-start UI entry points before deciding exact wiring locations — this plan's earlier
Spec text describes a "New Ticket" flow and a board that may not exist as literal components yet
on this branch; if they don't exist yet, wire the banner into whatever the *current* real
session-start entry point is, and note in your report that the New-Ticket-specific wiring is
Part 3's job once that UI exists). Depends on Task 7.

Banner: shown when `GET /org-settings` reports no `credential_configured`. Copy per the Spec's
wireframe: admin sees "Open Settings →" linking to the new org settings page; non-admin sees "ask
an org admin to configure a provider" with no action link. Determine the real admin/non-admin
signal the same way Task 7 had to (verify against this codebase's actual authorization pattern,
don't assume one).

Self-check: N/A — confirm build/typecheck.

---

## Task 9: Final verification sweep

**Files:** none changed — report only, mirroring Part 1's Task 12 exactly. Depends on all of
Tasks 1-8.

1. Real DB migration applied against a real (or realistically-local) SQL Server instance — confirm
   `0003_create_org_settings.sql` actually runs, the single-row constraint actually holds under a
   concurrent-write attempt if practical to test.
2. Real credential save-and-validate round trip against Task 2's real vault (a test/sandboxed
   credential is fine, matching Part 1's Task 12's own "no real prod credentials" posture) —
   confirm the RBAC scope is genuinely narrow (inspect the actual generated ARM JSON's role
   assignment scope, per Ruling 1's own stated verification requirement, not just that bicep
   compiles).
3. Real provider-switch test: save Copilot, provision a session, confirm it runs on Copilot; save
   Claude while that session is still in-flight, confirm the in-flight session's own behavior is
   unaffected (this is the one behavior Ruling 2 exists entirely to guarantee — test it for real,
   not just by code inspection); provision a *second* session after the switch, confirm it picks up
   Claude — all without a redeploy (confirm literally no container restart/redeploy step was
   needed anywhere in this sequence, since that's the whole point of Part 4).
4. Confirm the fallback path: a fresh deployment with no `org_settings` row falls back to
   `AGENT_PROVIDER`/`agentProvider`'s deploy-time default correctly.
5. Confirm a pre-Part-4 checkpointed run (if one can be constructed/simulated) resumes correctly
   under Task 4's bridge-fallback logic, not just a freshly-started one.
6. Write a final report naming what was verified for real, what remains unverified and why, and
   any place reality diverged from what Tasks 1-8 assumed — same shape as Part 1's Task 12 report.
