# Part 3 — Projects, tickets, board, and AC lifecycle: task breakdown

Spec (binding authority): `C:\Users\jblis\.claude\plans\inside-the-staging-container-sunny-tome.md`
— Part 3's own section, plus the "Rollout and sequencing" section's reasoning for why Part 3 ships
third (after Part 1 and Part 4, before Part 2). This file argues from that Spec but is grounded in
what Parts 1 and 4 *actually built* on `feature/claude-support` (not the Spec's description of code
written before either landed) — ground truth gathered via dedicated research
(`docs/superpowers/plans/part-3-research-notes.md`, read in full before drafting this) rather than
assumed from the Spec's own prose. Several real divergences were found and are recorded as Rulings
below, not silently absorbed. One of them (Ruling 3) is safety-critical, not a nice-to-have: the
research proved the current codebase would silently corrupt data the moment this Part's own core
feature (a second ticket against an existing project) is used, absent that fix.

## What Parts 1 and 4 actually built that this plan builds on (verified 2026-08-23, see the
## research notes for full citations)

- **`dbo.sessions`** (`agent/db/migrations/0001_create_sessions.sql`) already has `current_stage`
  and `title` columns and an `async def create_session(session_id, *, owner, repo, user_login,
  source_branch, work_branch, title)` / `list_sessions(owner, repo, source_branch=None)` API
  (`agent/src/session_store.py`). One row per session; `session_id` (a GUID) already doubles as
  the LangGraph `thread_id` and the sandbox session id. **A LangGraph thread already == one full
  pipeline run** — this is exactly what the Spec calls a "ticket." There is no separate ticket
  concept to invent; there is a **grouping key** to add above it (Ruling 1).
- **`GraphState`** (`agent/src/graph.py:159-268`, 19 keys, read in full) has no `project_id`.
  `StageSpec` (same file, lines 795-961, read in full) already has `hydrate_from_repo_file:
  Callable[[str, GraphState, SandboxProvider], Awaitable[dict[str, Any] | None]] | None` as a
  real field — **wired up for exactly one of the 8 stages (`tech-stack`)**, the other 7
  (`specification`, `plan`, `ac-to-tests`, `minimal-code-to-green`, `remediation`,
  `adversarial-compliance`, `metrics-exit`) leave it `None`. `hydrate_tech_stack_from_repo_file`
  lives in **`agent/src/preflight_nodes.py:397`, not `app_discovery.py`** — a design doc that
  assumed the latter is wrong about the file; correct target for Task 6/7 below.
- **`agent/src/spec_ledger.py`**: `EntryStatus = Literal["active", "retired", "revised"]`
  (line 37); `allocate_next_id` is genuinely reuse-proof (scans every entry ever recorded,
  retired included). `sync_ledger`'s auto-retire step (lines 218-220, exact quote) —
  ```python
  for entry in updated:
      if entry["id"] not in touched_ids and entry.get("status") in ("active", "revised"):
          entry["status"] = "retired"
  ```
  — retires **every** ledger entry the current draft didn't cite, with **zero ticket/project
  scoping**, because none exists yet. This is safe today only because there is exactly one draft
  in flight per repo, ever. **The research's own top finding: this is a live landmine for this
  Part specifically** — the moment ticket #2's Speccing stage runs against a project that already
  has ticket #1's user stories/ACs on the books, its draft (scoped to ticket #2's own feature)
  won't cite ticket #1's ids, and this unmodified logic retires ticket #1's *entire* AC set as
  "no longer touched." Not a missing feature; existing, currently-passing logic that misfires
  destructively the instant multi-ticket-per-project is possible. See Ruling 3 — fixing this is
  a prerequisite for shipping anything else in this Part, not an optional enhancement alongside it.
- **`agent/src/gates/ac_coverage_gate.py`**: `check_ac_coverage` already filters to `status in
  ("active", "revised")` (lines 634-636) — a retired AC already drops out of required coverage on
  its own. **No change needed here.** `id_variants()`/`ac_ids_in_name`/`attributed_ac_ids` (the
  id-in-test-name matching a retirement flow would reuse to find a retired AC's tests) are real,
  confirmed working, and **live in `agent/src/test_results.py`, not `ac_coverage_gate.py`**
  (`attributed_ac_ids` line 77, `ac_ids_in_name` line 105) — `ac_coverage_gate.py` only imports
  and calls them.
- **`agent/src/gates/write_scope_gate.py`** enforces scope via a **path-pattern classifier**
  against `git diff --name-only <baseline>` (lines 38-57, 140-199) — there never was a
  `_WRITE_TOOL_NAMES` allowlist constant (grepped repo-wide, zero matches). A tool-level "Layer 1"
  *did* exist once and was deliberately removed in Part 4 (no CLI-exec equivalent to translate it
  into — confirmed live in `claude_chat_model.py:294-300`/`copilot_chat_model.py:353-355`, both of
  which now just log a warning and proceed). **No delete/remove-file tool exists anywhere** in any
  stage's toolset today (confirmed: ac-to-tests' draft tools are `view/grep/glob/edit/create/
  apply_patch/skill`, `graph.py:1074-1077`) — and the gate's own docstring says this is
  *deliberate*, not an oversight: no delete tool means "revert these files" can never deadlock a
  stage, so `check_write_scope` does its own filesystem revert instead of asking the model to
  clean up after itself. Adding a delete capability (Task 8) has to respect why that boundary
  exists, not just paper over it.
- **`agent/config/models.yaml`** already has the `{stage}: {copilot: {...}, claude: {...}}`
  provider dimension (added in Part 1). Whatever this Part needs from `models.yaml`, if anything,
  is a **third** dimension layered on an already-two-dimensional shape, not a first one.
- **Frontend**: zero occurrences of "ticket", "board", "kanban", or "project" (case-insensitive)
  anywhere under `src/app/` — this really is greenfield UI work, not an extension of an existing
  concept with a different name. `/select` (`src/app/(boxed)/select/page.tsx`) is a flat
  repo → branch picker; `repo == project` today, full stop, confirmed by the frontend's own total
  absence of the word, not just inferred from missing UI. Session provisioning already tolerates
  **arbitrarily many concurrent sessions per repo** (`src/app/api/sessions/provision/route.ts`'s
  own comment: "Concurrency is fully open... each on its own branch") — the multi-ticket-per-repo
  case this Part needs is already load-bearing production behavior for "multi-session-per-repo,"
  just with zero grouping/labeling above the branch name yet.
- **`agent/src/git_ops.py`** (661 lines, read in full) never itself clones anything — every
  function operates on a clone that already exists inside a running sandbox. The actual clone is
  one line in `agent/sandbox-image/entrypoint.sh` (`git clone --branch ... --single-branch
  "$REPO_CLONE_URL" ...`), fed by `sessions_api.py:120`'s `repo_clone_url =
  f"https://github.com/{owner}/{repo}.git"`. **"Connect a Repository" for an existing repo the
  user can already push to is fully solved end-to-end today** — it's exactly today's provisioning
  path with a real `owner`/`repo`/`branch`, nothing new to build there. **Scaffolding a brand-new,
  not-yet-existing repo is genuinely absent** — grepped `git init` (zero matches, anywhere) and
  any GitHub create-repo REST call (zero matches; the one hit was an unrelated markdown
  instruction string inside a tech-stack template). `entrypoint.sh` has a no-clone "bare sandbox"
  escape hatch already (line 175) but it's for "no repo at all," not "create one and clone it."
  This is real, new infrastructure work (Ruling 6 / Task 3).
- **Migrations**: `0001_create_sessions.sql`, `0002_create_repo_vaults.sql`,
  `0003_create_org_settings.sql` exist. Next free number: **`0004`**.

## Ruling 1 — a "ticket" is a `dbo.sessions` row with a `project_id`, not a new parallel table

The Spec's prose treats "ticket" as a concept to introduce. The research shows it already exists,
under a different name: "every ticket runs the identical `StageSpec` set" is exactly what a
LangGraph thread / `dbo.sessions` row already does today, one-per-run. Inventing a separate
`dbo.tickets` table 1:1-joined to `dbo.sessions` would mean two tables representing one entity,
kept in sync forever for no benefit — the exact anti-pattern this whole redesign's Part 1 already
argued against for provider dispatch (two things that must always agree, with no mechanism forcing
them to). Instead: add one nullable-then-effectively-required `project_id UNIQUEIDENTIFIER` column
to `dbo.sessions` (Task 1). "Ticket" becomes the product-facing word for what the code calls a
session; `session_id` **is** the ticket id. `current_stage` (already a column) already gives the
board (Task 9) its column-placement signal for free — confirms this is the right cut, not just the
cheap one.

**Cost if wrong:** if a genuine ticket/session distinction turns out to be needed later (e.g. a
ticket that spans more than one session/thread), splitting them apart later is an additive
migration (add a real `tickets` table, backfill one row per existing session, add a FK) — no data
is lost by starting merged, since a session row already has everything a "ticket" needs.

## Ruling 2 — `dbo.projects`: `owner`/`repo` nullable until scaffolded, unique once known

```sql
CREATE TABLE dbo.projects (
    project_id      UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    name            NVARCHAR(200)    NOT NULL,
    owner           NVARCHAR(255)    NULL,   -- NULL until the repo exists (new-project path);
                                              -- set immediately (connect-repo path)
    repo            NVARCHAR(255)    NULL,
    tech_stack_id   NVARCHAR(100)    NULL,   -- catalog id (app_discovery.load_stack_catalog) or
                                              -- NULL for free-text / brownfield-detected
    tech_stack_text NVARCHAR(MAX)    NULL,
    created_by      NVARCHAR(255)    NOT NULL,
    created_at      DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME(),
    updated_at      DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE UNIQUE INDEX UX_projects_owner_repo ON dbo.projects(owner, repo)
    WHERE owner IS NOT NULL AND repo IS NOT NULL;
```

Why nullable: the "+ New Project" path (inline fields on the New Ticket form) creates a project
row from just a name + tech-stack choice — the GitHub repo behind it doesn't exist until the
ticket's own session actually provisions and scaffolds it (Ruling 6). Backfilling `owner`/`repo`
once the scaffold succeeds mirrors the exact idiom `sessions_api.py`'s own `provision_session`
already uses for `dbo.sessions.title` ("the real title arrives later, once scaffold_node has
requirements text to generate one from") — populate what's known now, backfill the rest once a
later step produces it, never a placeholder value. The filtered unique index (SQL Server supports
partial indexes) stops "Connect a Repository" from creating a second project for a repo that's
already connected, without that constraint ever colliding with the many simultaneously-`NULL`
not-yet-scaffolded new-project rows.

## Ruling 3 — `sync_ledger` drops silent auto-retire entirely; no mode parameter needed

**Corrected 2026-08-23, before Task 6 was dispatched**, after tracing the real call graph rather
than building on the Spec's own (reasonable-sounding, but unverified) two-mode design. Grepped
`sync_ledger(` across the whole codebase: there is exactly **one real call site**,
`graph.py`'s `_verify_specification_ledger` (the `specification` stage's own
`deterministic_verify`, `graph.py:583`). `brownfield-baseline` does not call `sync_ledger` at
all — it only writes `.ai-dev-workflow/manifest.json` (research §6) — so the Spec's premise that
`full_redraft` mode was needed "for brownfield-baseline" describes a call site that doesn't exist.

That leaves one real question: does the ONE call site ever need "auto-retire anything not
re-cited" as CORRECT behavior, for any project state? Traced it through: `sync_ledger`'s own
existing "greenfield leniency" branch already makes the auto-retire step a no-op whenever the
ledger starts empty (every id in a from-nothing draft is freshly allocated, so every id is
trivially in `touched_ids` — nothing pre-existing exists to wrongly retire). So the auto-retire
step was NEVER doing useful work on an empty ledger; the only condition under which it does
anything at all is exactly the dangerous one Ruling 3 exists to fix (a non-empty ledger, i.e. a
second-or-later ticket). There is no real scenario left where the old unconditional behavior is
both reachable and correct — a `mode` parameter would be a switch between "no-op" and "dangerous,"
which is not a real choice worth a parameter. Simpler fix, same safety, less machinery:

- `sync_ledger` drops the unconditional auto-retire loop entirely. An entry's status only ever
  changes to `"retired"` because the current draft *names* it — nothing is retired on silence,
  ever, for any project state. This needs no mode, no `GraphState` pinning field (nothing to pin —
  the behavior no longer depends on which situation this call is in), and no change to
  `sync_ledger`'s existing signature beyond what the next bullet adds.
- The Specification draft response schema gains `retired_ac_ids: list[str] = []` /
  `retired_us_ids: list[str] = []`. `sync_ledger` retires exactly the ids named here, plus whatever
  `existing_ac_id`/`existing_us_id` citations resolve as usual (a citation always meant "revise,"
  never "retire," and still doesn't) — nothing else, on every call, unconditionally.
- The Speccing prompt (Task 6, both a brand-new project's first pass and every later ticket) gains
  the same one instruction either way: state what this pass adds or changes; if something the
  ledger already has no longer belongs, name it in `retired_ac_ids`/`retired_us_ids` rather than
  just omitting it from the draft. A first-ever pass on an empty ledger has nothing to name and the
  fields stay empty — same effective behavior as today, just reached by an explicit empty list
  instead of an implicit "nothing existed to retire" fact about the ledger's starting size.
- **Task 6's own job, not assumed here**: confirm, by reading the specification stage's actual
  draft/audit prompts (not guessed), whether one ticket's own multiple internal draft→audit→verify
  cycles restate that ticket's own scope in full on every cycle. If they do (expected — it's the
  same work session iterating on the same piece of work), this fix has no within-ticket regression:
  an ordinary revision cycle still says everything it currently means to say, and
  `retired_ac_ids`/`retired_us_ids` covers a deliberate drop the same way a citation already covers
  a deliberate revision. This is a real thing to verify, not an assumption to ship on.

**Explicitly deferred, not part of this Part** (Non-Goals section has the full list): the
supersession *lineage* fields (`superseded_by`/`supersedes`) and the prompt-level "revise-in-place
vs. retire-and-replace" nuance the Spec also describes. Both are real and both are absent today
(research confirmed zero matches for either field, repo-wide) — but neither is safety-critical the
way the mode split is. A retired-with-no-lineage-link AC is inert and safe, just less traceable in
a future report; that traceability gap is a reporting nicety layered on top of a fix that must
exist regardless. Building it now would be exactly the kind of premature scope the Spec itself
warns against elsewhere (git-worktree sharing, the pre-warm checkbox below) — revisit once
multi-ticket projects are running in production and a real report actually needs the lineage.

## Ruling 4 — the "pre-warm the cache" checkbox ships as a Non-Goal for this pass

Connect-Repository's wireframe includes an optional checkbox: run tech-stack detection +
baselining immediately on connect, instead of deferring to whichever ticket needs it first. The
Spec's own fallback behavior for the *unchecked* case — "the same detection just runs on whichever
ticket needs it first — nothing skipped, only deferred" — is already fully correct and is the ONLY
behavior this Part ships. Building the checked path requires a partial-pipeline execution mode
(run tech-stack + brownfield-baseline in isolation, with no feature ticket wrapping them) that
does not exist in `graph.py` today and touches its routing logic for a pure latency-hiding
convenience, not a correctness need. Cut for this pass; "Connect a Repository" always behaves as
if unchecked. Revisit once real usage shows the first-ticket latency hit is actually a problem
worth the routing change.

## Ruling 5 — the Board ships with plain polling, not a live AG-UI subscription

The Spec's own board wireframe says a card's column updates live, "no page refresh... falls
directly out of Part 2's transport decision" (`useAgent`'s subscription). That is a real
sequencing conflict inside the Spec itself: Part 2 — which is supposed to *resolve* the
CopilotKit/transport question using Part 1 and Part 3 as production evidence — hasn't happened yet
at this point in the rollout order (1 → 4 → 3 → 2), and the Spec's own rollout section says exactly
that: Part 2's transport question should be "resolved with Part 1/3 running in production as
evidence, not decided speculatively up front." Building a live-subscribed board now means either
deciding Part 2's open question early (contradicting the Spec's own stated reason for sequencing
it last) or writing bespoke live-transport code Part 2 would need to replace. This Part's Board
polls `GET /sessions?owner=&repo=` (extended to accept `project_id`, Task 2) on a fixed interval
and on window focus — correct, simple, and Part 2 upgrading it to live updates later is a pure
addition to the data-fetching layer, not a rewrite of the board's column logic or data model.

## Ruling 6 — new-repo scaffolding: personal account only, repo name == project name

"+ New Project" needs an actual GitHub repo to exist before any session can provision against it.
The wireframe shows no owner picker (no org selector) — so scaffolding always creates the repo
under **the signed-in user's own personal GitHub account** (their login, via `POST /user/repos`
with the user's existing OAuth token — the exact token `sessions_api.py`'s `ProvisionRequest.
github_token` already carries), never an org. The repo's name is the project's `name` field,
slugified (lowercased, spaces → hyphens, stripped of characters GitHub rejects) — no separate
"repo name" field, matching the wireframe. A name collision surfaces as GitHub's own real 422,
shown to the user verbatim (same "fail fast with the provider's own error" precedent
`org_settings_router`'s credential probe and the per-repo vault PUT both already use) — no
client-side pre-check duplicating GitHub's own validation. Org-owned new projects are a real,
larger feature (needs an org picker, needs to check the user's role in that org) explicitly out of
scope here.

## Ruling 7 — added 2026-08-23, during Task 7a: `ac_coverage_gate.py` needs its own ticket-scoping
## fix, same shape as Ruling 3's, and it is just as blocking

Found by Task 7a while investigating ac-to-tests' own hydrate/reframe check, not assumed from the
Spec: `agent/src/gates/ac_coverage_gate.check_ac_coverage` computes `active_ac_ids` from the
**entire project's** ledger (`ac_coverage_gate.py:630-636`, unscoped by ticket — every entry with
`status in ("active", "revised")`, project-wide) and requires each one to have a currently
**failing** test (`tautological = [ac for ac in active_ac_ids if ac_line_status.get(ac) ==
"pass"]`, line 752) — the mechanical check for TDD's RED step. This is correct and necessary for
a single-ticket project. It is a hard, deterministic, 100%-reproducible **blocker** for every
second-or-later ticket on any project with prior shipped work: the moment ticket #1's feature is
merged and its test is legitimately, correctly green, ticket #2's own ac-to-tests stage runs the
whole suite, sees ticket #1's already-passing test, and fails the gate with "these ACs' tests are
already PASSING with no implementation yet" — about a ticket #2 never touched and has no way to
make red again without breaking ticket #1's shipped feature. Every downstream check in this
function (`missing`, `depth_shortfall`, `unattributed_tests`) reads from the same unscoped
`active_ac_ids`, so the same blindspot runs through all of them, not just the tautological check.

This is the SAME shape of bug Ruling 3 fixed for `spec_ledger.sync_ledger` — an existing mechanism
built when "one thread = one project's whole spec" was the only case, now wrong the instant a
project can receive more than one ticket — just in a different gate, and worse in kind: Ruling 3's
bug silently corrupted data; this one deterministically **halts all further progress on a project**
the moment its first ticket ships. It must be fixed as part of this Part, not deferred — deferring
it would ship a Part whose own stated purpose (more than one ticket per project) cannot work past
the first ticket.

**The fix**: scope `active_ac_ids`, once, right after it's computed, down to only the ACs the
*current ticket's own* approved Specification actually lists — everything downstream already
consumes `active_ac_ids` uniformly, so fixing it at the source fixes every downstream check in one
place. The required set still has to come from an independent source, not from the model's own
`content_dict`/`coverage_plan` claims (the gate's whole point is verifying the model's self-report
deterministically — trusting the model's own plan for which ACs count would let it simply omit an
inconvenient one). "This ticket's own approved Specification's AC ids" is exactly what Task 7a's
own new `spec_ledger.hydrate_ac_to_tests_ticket_mode_context` already had to compute for a
different reason (deciding whether to show the reframing segment) — Task 7c (below) should reuse
or factor out that same computation rather than deriving it a second, possibly-diverging way.

**Not this Part's job to solve**: whether ticket #2 should ALSO be checked for regressions against
ticket #1's already-shipped, now-scoped-out tests. That is a real, different question (this gate's
own docstring frames its whole job as "TDD's RED step," which is inherently ticket-scoped — you
cannot make a shipped ticket's test red again without breaking that ticket) and, if it needs
answering at all, belongs to whichever stage already owns whole-project regression protection
(e2e/metrics-exit), not this gate. Scoping this gate down does not remove regression protection
that already exists elsewhere; it only stops this gate from wrongly blocking on a fact (an older
ticket's test is green) that was never wrong in the first place.

## Global Constraints (apply to every task)

- **Every ticket runs the identical 8-stage `StageSpec` set, always** (`tech-stack`,
  `specification`, `plan`, `ac-to-tests`, `minimal-code-to-green`, `remediation`,
  `adversarial-compliance`, `metrics-exit`) — no stage-skipping based on ticket type. What varies
  per ticket is entirely internal to each stage's own hydrate/cache check (Task 6/7); the graph's
  topology itself does not branch on "is this ticket's project new or existing."
  `brownfield-baseline` stays the separate one-time pre-stage it already is (routed by
  `_route_after_tech_stack`, `graph.py:2390-2403`) — this Part does not change that routing.
- **No stage's existing draft → audit → gate structure changes.** A cheaply-hydrated stage still
  gets confirmed by a second, different model exactly as a freshly-drafted one would — hydrate
  checks decide how much a stage's *draft* has to do, never whether audit or the human gate run.
- **`GraphState.provider`'s pinning discipline (Part 4, Ruling 2) is unaffected and must stay
  that way.** Nothing in this Part re-introduces a live-resolution call inside a per-run code path.
- Every new DB access goes through `session_store.py`/a sibling module in the exact same
  `aioodbc`-via-`db.py` pattern already established — no new database access pattern.
  Migration file for this Part is `agent/db/migrations/0004_create_projects.sql`.
- Every new backend route lives in `agent/src/sessions_api.py` as a new `APIRouter` (matching the
  existing `router`/`config_router`/`org_settings_router`/`catalog_router` convention in that
  file) unless a task below says otherwise.
- Frontend: raw Tailwind, no component library, `SaveState`-style discriminated unions for
  async forms — matching every existing settings page in this codebase, not a new convention.
- Do not build anything under Ruling 3's deferred-lineage list, Ruling 4's checkbox, or Ruling 5's
  live-subscription — each is explicitly out of scope for this pass; implementers who find
  themselves reaching for one of these should stop and flag it rather than build it.

## Task 1: DB migration — `dbo.projects` + `dbo.sessions.project_id`

`agent/db/migrations/0004_create_projects.sql`: the `dbo.projects` table exactly as specified in
Ruling 2, plus `ALTER TABLE dbo.sessions ADD project_id UNIQUEIDENTIFIER NULL REFERENCES
dbo.projects(project_id);` and a supporting index (`CREATE INDEX IX_sessions_project ON
dbo.sessions(project_id, started_at DESC);` — mirrors the existing `IX_sessions_repo_recent`
shape) for the board's own project-scoped listing query.

**Verify before finalizing this migration, don't guess:** whether the board (Task 9) needs a
"paused, awaiting gate" signal distinct from `current_stage` alone (the wireframe's `⏸` marker).
Read how `graph.py`'s `interrupt()`-based gate mechanism and `session_store.update_current_stage`
interact — does anything already distinguish "drafting stage X" from "paused at stage X's gate,"
or does that require a new column here (e.g. `awaiting_gate BIT NULL`)? Decide and document
whichever answer is true; do not add a speculative column before confirming it's actually needed.

`agent/src/session_store.py`: extend `create_session`'s signature with a required `project_id:
str` keyword parameter (every session created from this point on belongs to a project — Ruling 1);
extend `list_sessions` to accept an optional `project_id` filter alongside its existing
`owner`/`repo`/`source_branch` ones, for the board's query (Task 9).

New module `agent/src/project_store.py` (sibling of `session_store.py`, same `db.py` pattern):
`create_project(name, *, tech_stack_id, tech_stack_text, created_by) -> project_id` (owner/repo
start `NULL`), `set_project_repo(project_id, owner, repo) -> None` (the post-scaffold backfill,
Ruling 2), `get_project(project_id) -> dict | None`, `list_projects() -> list[dict]`,
`find_project_by_repo(owner, repo) -> dict | None` (backs Connect-Repository's "already connected"
check against the unique index).

## Task 2: Backend — `projects_router` + New-Ticket provisioning path

`agent/src/sessions_api.py`: new `projects_router = APIRouter(prefix="/projects",
tags=["projects"])`.

- `GET /projects` — list projects (New Ticket form's project picker).
- `POST /projects` — body `{name, tech_stack_id, tech_stack_text, created_by}`; creates a project
  row with `owner`/`repo` still `NULL` (the "+ New Project" inline-fields case). Returns the new
  `project_id`.
- `POST /projects/connect` — body `{owner, repo, created_by}`; the Connect-Repository action.
  Calls `find_project_by_repo` first (idempotent — connecting an already-connected repo returns
  the existing project rather than erroring or duplicating, matching this codebase's existing
  idempotent-creation convention in `provision_session`); otherwise creates a project row with
  `owner`/`repo` already set and `tech_stack_id`/`tech_stack_text` both `NULL` (unknown until
  brownfield detection or a later Tech Stack confirmation runs — not this task's job to guess it).

Extend `ProvisionRequest` (existing model, same file) with a required `project_id: str`. In
`provision_session`, after the existing vault-fetch block and before computing `work_branch`: if
the named project has no `repo` yet (the "+New Project" case — look it up via
`project_store.get_project`), call the new repo-scaffolding step (Task 3) using `body.owner`
(computed from the signed-in user's own login, forwarded by the frontend BFF the same way
`user_login` already is) and the project's `name`; on success, call `set_project_repo` and use the
now-known `owner`/`repo` for the rest of provisioning exactly as today. If scaffolding fails,
return a 502 with GitHub's own error detail (same convention as the existing `except Exception`
branch around `provider.provision(...)` a few lines below) — do not create a `dbo.sessions` row
for a ticket whose repo was never actually created.

`session_store.create_session`'s new `project_id` argument is threaded through from
`body.project_id` at this same call site.

Extend `list_sessions`'s existing route (`GET /sessions`) to accept an optional `project_id` query
param, passed straight through to the now-extended `session_store.list_sessions`.

## Task 3: GitHub repo scaffolding (new infrastructure — confirmed absent by research)

New module `agent/src/repo_scaffold.py`: `async def create_repo(name: str, github_token: str) ->
dict` — `POST https://api.github.com/user/repos` with `{"name": <slugified name>, "private":
true}` (private by default — this is generated/AI-authored code, matching this codebase's own
security-conscious defaults elsewhere; no UI toggle for this in the wireframe, so no toggle is
built), using the same Bearer-token REST pattern `git_ops.py`'s `open_pull_request`/
`delete_remote_branch` already establish. Returns `{owner, repo, clone_url}` from GitHub's own
response on success; raises with GitHub's real error body (a name collision's 422 included) on
failure — no client-side name-availability pre-check (Ruling 6).

`agent/sandbox-image/entrypoint.sh`: today's clone step is unconditional
(`git clone --branch "$REPO_BRANCH" --single-branch "$REPO_CLONE_URL" "$WORKSPACE_DIR"`). Add a
new mode, selected by a new env var (e.g. `SCAFFOLD_NEW_REPO=1`, set only by the provisioning path
Task 2 added, never by the ordinary Connect-Repository/`/select` paths): `git init`, set `origin`
to the newly-created repo's clone URL, create an initial empty commit (`README.md` naming the
project, so the very first push isn't rejected as empty by branch-protection-style expectations
elsewhere in this pipeline), and push. Reuse whatever credential-injection mechanism the existing
clone step already uses for `REPO_CLONE_URL`'s auth (read that mechanism before assuming it's
identical for a push vs. a clone — confirm rather than guess, per this Part's own house style).

## Task 4: Frontend — the New Ticket form (single intake path)

New route, e.g. `src/app/(boxed)/tickets/new/page.tsx` (or a modal from `/select` / a new
project-scoped landing page — implementer's call on exact placement given no existing page to
extend; keep it reachable from wherever `/select`'s old "start new session" affordance lived).
Matches the Spec's own wireframes: a Project picker (`GET /projects`, plus a synthetic
"+ New Project" option); selecting "+ New Project" reveals inline `name` + tech-stack fields
(catalog picker via the existing `GET /tech-stack-catalog` route, or free-text) directly on the
same form, no separate wizard screen. `Title`/`Description` fields always present. Submitting:
if an existing project was chosen, call the existing provisioning flow directly (mint a
`sessionId`, call `/api/sessions/provision` exactly as `SandboxSessionBoot.tsx` already does,
`project_id` = the chosen project's id); if "+ New Project," first `POST /api/projects` (new BFF
route mirroring the settings-organization proxy pattern: server-side token forwarding, no client
secret handling) to get a `project_id`, then provision exactly the same way.

Raise the "no coding-agent credential configured" banner (`src/lib/settings-checks.ts`, Part 4)
here too if `session_ready` is false — this is exactly the point the Spec's own wireframe shows the
banner triggering from ("New Ticket's Assign button... whether or not it's also creating a
project").

## Task 5: Frontend — Connect a Repository (expanded 2026-08-23, after Task 4's own findings)

New route/modal calling `POST /api/projects/connect` (already built ahead of schedule by Task 4,
per a dispatch imprecision on the controller's own part — reuse the existing file, do not
recreate it) with the chosen `owner`/`repo` (reusing whatever existing repo-listing UI `/select`
already has for picking a GitHub repo — do not rebuild that picker). Ships without the pre-warm
checkbox (Ruling 4) — connecting just creates the project row; the first ticket filed against it
runs tech-stack/baseline detection exactly as any ticket on a brand-new project would.

**Two real gaps Task 4 found and correctly left for this task, both now mandatory scope:**

- **`/select`'s own existing "start new session" flow provisions with no `project_id`** — broken
  by Task 2's own backend change (the field became required), not by Task 4. This task is exactly
  the one scoped to "repurpose `/select`'s own picker," so fixing this is this task's job, not a
  separately-ledgered gap. Investigate the real current `/select` page and `SandboxSessionBoot.tsx`
  before deciding the fix's exact shape, but the controller's own lean (not a mandate — confirm
  against real routing/usage before committing): route `/select`'s existing "start new session"
  action through the SAME connect-or-find-existing-project step this task is building (call
  `POST /projects/connect` with the picked `owner`/`repo` to get a real `project_id` back, THEN
  provision) rather than maintaining `/select` as a second, parallel path that has to stay in sync
  with the New Ticket form forever. If investigation shows a cleaner resolution, use it — just
  don't leave `/select` broken.
- **New-ticket sessions hardcode branch `"main"`**, correct only for a freshly-scaffolded repo
  (Task 3 always creates `main`) and wrong for a connected repo with a different real default
  branch — `dbo.projects` has no column for it. Add one: a small migration
  (`agent/db/migrations/0005_add_project_default_branch.sql`, next free number after `0004`) adding
  `default_branch NVARCHAR(500) NULL` to `dbo.projects`. Populate it at connect time — GitHub's own
  `GET /repos/{owner}/{repo}` response includes a `default_branch` field; fetch it (or extend
  `POST /projects/connect`'s existing backend handler to fetch it) and pass it into
  `project_store`'s connect path. Have the New Ticket form's provisioning call use
  `project.default_branch` when set, falling back to `"main"` only when it's genuinely absent
  (the scaffold case, or a pre-migration row) — replacing the hardcoded literal, not adding a
  second, competing source of truth for it.

## Task 6: `sync_ledger` explicit-retirement fix + ticket-mode Speccing

Implements Ruling 3 exactly: `sync_ledger` drops its unconditional "retire anything not re-cited"
loop entirely — no mode parameter, since there is exactly one real call site
(`graph.py:583`, `_verify_specification_ledger`, the `specification` stage's own
`deterministic_verify` — confirmed by grep, not assumed; `brownfield-baseline` never calls this
function at all). The Specification draft schema gains `retired_ac_ids: list[str] = []` /
`retired_us_ids: list[str] = []`; `sync_ledger` retires exactly the ids named there (plus whatever
`existing_ac_id`/`existing_us_id` citations already resolve as revisions), on every call,
regardless of project/ticket state. Confirm Ruling 3's own open verification item while you're in
this code: read the specification stage's actual draft/audit prompts and confirm one ticket's own
multiple internal draft→audit→verify cycles restate that ticket's own scope in full each cycle
(expected, but verify — don't assume) — this is what makes dropping silent auto-retire safe with
no within-ticket regression, not just safe across tickets.

Also this task's job: the Speccing stage's own `hydrate_from_repo_file` — does a cached baseline
spec (`.ai-dev-workflow/spec/ledger.json` already existing and non-empty) mean "expand this
ticket's text against the existing baseline, scoped" rather than "do the full read"? This is the
one hydrate check the Spec calls out by name as distinct from the other 5 in Task 7 below (it's a
prompt-framing switch on the *existing* Speccing prompt, not a new draft-vs-generate decision the
way the others are) — build it here, alongside the retirement-field plumbing it sits next to, not
in Task 7.

## Task 7: Per-stage hydrate audit — Plan, ac-to-tests, Minimal-Code-to-Green, Remediation,
## Adversarial Review, Metrics Exit (6 stages)

For each of these 6 stages, answer and implement the same question Tech Stack's own
`hydrate_from_repo_file` already answers for itself: does this stage's own expected artifact
already exist for this project, in a form this ticket can cheaply confirm/extend rather than
generate from scratch? The Spec names 5 explicitly (Plan: existing architecture doc to extend?
Minimal-Code-to-Green: existing conventions to extend rather than assuming a blank repo?
Remediation, Adversarial Review, Metrics Exit: scope output to what *this ticket* changed rather
than re-auditing the whole project every time) — **this plan adds ac-to-tests as a 6th**, not
named in the Spec's own list, on the same reasoning: a ticket's failing-tests stage should
generate tests for *this ticket's own* new/changed ACs, not regenerate or re-verify the whole
project's existing test suite every time, exactly the same over-scoping risk the Spec already
flags for Adversarial Review and Metrics Exit.

This is genuinely 6 separate judgment calls, not one mechanical edit repeated 6 times — each
stage's own real artifact and cache signal differs (an architecture doc's path and freshness
signal is not a test suite's). Dispatch as one task if a single implementer can reasonably carry
6 hydrate designs at once; split into multiple tasks (2-3 stages each) if that turns out too large
once underway — either is fine, ledger the actual split chosen and why.

## Task 7c: `ac_coverage_gate.py` ticket-scoping fix (Ruling 7 — added 2026-08-23, blocking)

Implements Ruling 7. In `agent/src/gates/ac_coverage_gate.py`'s `check_ac_coverage`, filter
`active_ac_ids` (currently the whole project's ledger, lines 630-636) down to just the ids that
belong to the *current ticket's own* approved Specification, immediately after it's computed —
before it feeds `tautological`/`missing`/depth/attribution checks, all of which already read it
uniformly, so one filter at the source fixes every downstream use.

Getting "this ticket's own AC ids" needs the same independent-source discipline
`hydrate_ac_to_tests_ticket_mode_context` (Task 7a, `spec_ledger.py`) already established for the
identical question, asked for a different reason — read that function first and reuse its
approach (or factor the shared computation into one function both call) rather than deriving this
a second, possibly-diverging way. Trace `check_ac_coverage`'s real call chain before assuming
where to plumb this from: it's called from `agent/src/gates/write_scope_gate.py:299`
(`verify_ac_to_tests`, the `ac-to-tests` stage's actual `deterministic_verify` — confirmed by grep,
not the stage name alone), which has `thread_id`/`provider` but not a `GraphState` — so reading
the current ticket's own approved Specification will most likely mean a sandbox file read (same
`repo_files.read_repo_file` pattern every other stage-file check in this codebase already uses),
not a new parameter threaded through five call sites. Find the actual current-ticket Specification
file path (the same numbered-stage-file convention `workflow_persistence.py` already uses for
`PLAN_APPROVED_PATH`/`TECH_STACK_APPROVED_PATH` — confirm the Specification stage's own persisted
path rather than guessing it) rather than inventing a new persistence location.

Do NOT build the "should ticket #2 also be checked for regressions against ticket #1's already-
shipped tests" mechanism — Ruling 7 explicitly scopes that out of this task; it's a different
question belonging to whichever stage already owns whole-project regression protection, not this
gate.

Verify the actual fix, not just that the code compiles: reproduce Ruling 7's exact failure
scenario against a real or realistic fixture (ticket #1's AC test recorded as passing in a
structured report, ticket #2's own new AC recorded as failing) and confirm `check_ac_coverage` now
passes on ticket #2's own coverage while correctly still requiring ticket #2's own new AC to be
red — not just that ticket #1's AC stops being flagged.

## Task 8: Delete-tool capability for retiring-ticket test cleanup

The Spec's own flagged gap: once `sync_ledger` (Task 6) retires an AC, its test(s) don't remove
themselves. `write_scope_gate.py`'s existing path-pattern classifier (not a tool-name allowlist —
see the grounding section) may already handle a *deleted* path exactly like an edited one, since
`check_write_scope` classifies by matching the path string against the same test-pattern regexes
either way — **confirm this by reading `check_write_scope`'s exact git-diff handling before
assuming it**; do not add gate logic that already exists. The real, confirmed-absent piece is
narrower: no stage's toolset includes a delete-capable tool at all, so the model can never attempt
a removal regardless of what the gate would allow. Add one, scoped to whichever stage(s) do
test-retirement work (most likely `minimal-code-to-green`/`remediation`, whichever currently
authors/edits test files for a ticket) — and confirm deliberately that this doesn't reopen the
exact deadlock `write_scope_gate.py`'s own docstring says the *absence* of a delete tool was built
to prevent ("revert these files" deadlocking the stage): a delete tool that's freely available for
every path, not scoped by the same test-pattern check the rest of this gate already uses, would be
exactly that regression. If this turns out larger than "one new tool + one verification," it is
still real, still small enough to finish as its own task, but not to expand un-ledgered.

## Task 9: Frontend — Board (project-scoped, stage columns, polling)

New route, e.g. `src/app/(boxed)/projects/[projectId]/board/page.tsx`. Columns are the 8 real
stage keys (not the Spec wireframe's abbreviated legend, which folds `metrics-exit` in with
Adversarial Review and adds two non-pipeline columns, "Ready for Review"/"Done," that don't
correspond to any real `StageSpec` — use the actual 8-stage list from `graph.py`'s `STAGES`, plus
whatever this Part's own Task 1 verification decided for a terminal "done" bucket, e.g. sessions
with `status="completed"`). One card per session/ticket (`GET /sessions?project_id=`, extended
Task 2), column = `current_stage`, `⏸` marker = whatever Task 1's verification found for
"awaiting gate." Polls on a fixed interval (e.g. 15s) and on window focus (Ruling 5) — no
CopilotKit/AG-UI subscription wiring in this task.

## Task 10: Final verification sweep

Mirrors Part 4's own Task 9: a real, non-mocked pass proving the parts that matter most under
real conditions, not just unit-level self-checks. At minimum:

1. **The safety-critical one**: on a real (or realistic local) DB, create a project, file ticket
   #1 through Speccing far enough to get real US/AC ids on the ledger, then file ticket #2 against
   the *same* project and run its own Speccing stage without `retired_ac_ids`/`retired_us_ids`
   naming ticket #1's ids — confirm ticket #1's ids are still `active`/`revised` afterward, not
   silently `retired`. This is the exact failure the research proved would happen without Ruling
   3's fix; prove it's actually fixed, empirically, not just that the code compiles.
2. New-repo scaffolding end-to-end against a real (or throwaway test) GitHub repo: create, clone
   inside a real sandbox, first commit, push — confirm the whole chain the Spec's wireframe
   assumes actually works, not just that `repo_scaffold.create_repo` returns 201.
3. Connect-Repository idempotency: connecting the same `(owner, repo)` twice returns the same
   `project_id` both times, never a duplicate row (Ruling 2's unique index actually holds under a
   real concurrent-ish attempt, not just in isolation).
4. Every one of the 8 stages' hydrate checks (Task 6/7) actually skips or shortens its draft on a
   warm cache, and still runs a real draft + a real, different-model audit on a cold one — for at
   least one stage besides Tech Stack, prove this against a real warm project, not by reading the
   code and asserting it should work.

## Non-goals (this pass)

- Supersession lineage (`superseded_by`/`supersedes`) and the "revise vs. retire-and-replace"
  prompt nuance — Ruling 3.
- The "pre-warm the cache" checkbox on Connect-Repository — Ruling 4.
- A live, AG-UI-subscribed board — Ruling 5. Polling only.
- Org-owned new-project scaffolding — Ruling 6. Personal account only.
- Git-worktree-shared sandboxes across concurrent tickets on one project — already a Non-Goal in
  the Spec itself ("don't build this now... revisit as a follow-up optimization"); nothing in this
  Part's task list revisits it. Each ticket still gets its own full clone/container.
- Anything from Part 2's own scope (transcript/tool-call redesign, swimlane, diff-primitive, Gate
  UI overhaul) — this Part's Board is a list of cards in columns, not a run-detail view. Clicking
  into a card in this pass can link to today's existing workflow page unchanged.
