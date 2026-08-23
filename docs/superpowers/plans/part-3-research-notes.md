# Part 3 research notes: current-state ground truth (post Part 1 & Part 4)

**Ref used:** all citations below are against the local branch `feature/claude-support` at commit
`3c0ade2223ddfc1dbae001a2684090bf363091da`, read via `git show feature/claude-support:<path>` (and
`git grep ... feature/claude-support`) rather than off the working tree.

**Methodology note (read this first):** the worktree provisioned for this task
(`.claude/worktrees/agent-a5c7c96d6aca8485e`) was checked out at commit `6c51d09` ("new
specificztion") on a branch called `worktree-agent-a5c7c96d6aca8485e`. `git merge-base` confirms
`6c51d09` is a strict ancestor of `feature/claude-support`'s tip (`3c0ade2`) — the worktree's own
history is only 4 commits deep (`3549874` Initial commit → `b209adf` → `b04ebd4` → `6c51d09`) and
predates essentially everything, including all of Part 4. That worktree's files on disk are **not**
representative of current `feature/claude-support` and were not used. Instead, every file below was
read directly from the `feature/claude-support` ref via `git show`/`git grep` against the shared
`.git` object store (same repository, just addressed by ref instead of by working-tree path) — this
is still pure read-only inspection, nothing was checked out or modified. Every citation
(`path:line`) refers to that ref's content.

(Separately: this report was originally written to
`docs/superpowers/plans/part-3-research-notes.md` inside this task's own isolated worktree, not the
shared checkout path originally given — the harness refused a direct write to the shared checkout
from a worktree-isolated agent. The dispatcher copied it into the shared checkout at this same path
afterward — the worktree itself was disposed of once its one output file was extracted.)

---

## 1. `agent/src/spec_ledger.py`

Module docstring (lines 1–11) frames it as "P2's stable ID registry (US-####/AC-####.#), persisted
at `.ai-dev-workflow/spec/ledger.json`", deliberately independent of `graph.py`.

**`EntryStatus` (line 37):**
```python
EntryStatus = Literal["active", "retired", "revised"]
```
Exactly three members: `"active"`, `"retired"`, `"revised"`. (`EntryKind`, line 38, is
`Literal["user_story", "acceptance_criterion"]`.)

**`allocate_next_id` (lines 79–88):**
```python
def allocate_next_id(entries: list[dict[str, Any]], kind: EntryKind, parent_us_id: str | None = None) -> str:
    """Monotonic per kind, never reused even after retirement -- derived by scanning every entry
    ever recorded (retired ones are never physically removed from the list), not a separate
    counter field, so this is correct by construction rather than by keeping two things in sync.
    """
    if kind == "user_story":
        return f"US-{_next_us_number(entries):04d}"
    if parent_us_id is None:
        raise ValueError("acceptance_criterion allocation requires parent_us_id")
    return f"{parent_us_id}.{_next_ac_number(entries, parent_us_id)}"
```
The uniqueness guarantee is real, confirmed by reading the two helpers it calls:
```python
def _next_us_number(entries: list[dict[str, Any]]) -> int:
    numbers = [int(e["id"].split("-")[1]) for e in entries if e.get("kind") == "user_story"]
    return (max(numbers) + 1) if numbers else 1
```
(lines 65–67) and `_next_ac_number` (lines 70–76, same shape, additionally filtered by
`parent_us_id`). **Neither helper filters by `status` at all** — they scan every entry of the
matching `kind` regardless of whether it's `active`, `retired`, or `revised`. So yes: retired
entries are genuinely included in the max-number scan, which is what makes id reuse impossible by
construction rather than by convention.

**`sync_ledger` (lines 98–100):**
```python
def sync_ledger(
    entries: list[dict[str, Any]], draft_user_stories: list[dict[str, Any]], run_id: str
) -> LedgerSyncResult:
```
Three positional params, no keyword-only markers, returns `LedgerSyncResult` (frozen dataclass:
`passed: bool`, `reasons: list[str]`, `updated_entries: list[dict[str, Any]]`, lines 42–46).

**Auto-retire logic — exact quote (lines 215–222), reached only when `reasons` is empty (i.e. the
sync passed):**
```python
    if reasons:
        return LedgerSyncResult(passed=False, reasons=reasons, updated_entries=entries)

    for entry in updated:
        if entry["id"] not in touched_ids and entry.get("status") in ("active", "revised"):
            entry["status"] = "retired"

    return LedgerSyncResult(passed=True, reasons=[], updated_entries=updated)
```
So: on a passing sync, any entry (US or AC) currently `active` or `revised` that this draft did not
cite-or-create (`touched_ids`) flips to `retired`. There is no partial/selective retirement and no
distinction between "the human removed this on purpose" and "the model simply forgot to re-cite
it" — absence from the current draft is the only signal. On a failing sync (any `reasons`), nothing
is retired and the original `entries` are returned unchanged.

There is also a documented "greenfield leniency" branch (lines 123–133): when the incoming
`entries` list is empty, every story/AC's `existing_us_id`/`existing_ac_id` is force-nulled to `None`
before processing, treating every citation as "new" rather than failing on a hallucinated citation
against an empty ledger.

**`superseded_by` / `supersedes`:** grepped `superseded_by|supersedes` across the entire repository
tree at this ref (not just this module) — **zero matches anywhere**. No such field, shaped or
named, exists in the current data model. The only status-transition vocabulary that exists today is
the three-way `EntryStatus` above (`active`/`retired`/`revised`); "revised" already means "an
existing id had its title/description updated in place," which is adjacent to but not the same
concept as a supersession chain (there's no way today to say "US-0004 replaces US-0002" while
keeping both ids independently addressable).

---

## 2. `agent/src/gates/ac_coverage_gate.py`

**`check_ac_coverage`'s status filter (lines 634–636):**
```python
            active_ac_ids = [
                e["id"] for e in entries if e.get("kind") == "acceptance_criterion" and e.get("status") in ("active", "revised")
            ]
```
Confirms: `"active"` and `"revised"` count toward required coverage; `"retired"` is excluded. This
mirrors `spec_ledger`'s own status vocabulary exactly (no third bucket, no supersession-aware
filtering).

**`id_variants` (lines 96–110), exact signature and body:**
```python
def id_variants(ac_id: str) -> list[str]:
    """Spellings a test name may legitimately use for one ledger id. ..."""
    variants = {ac_id}
    if ac_id.startswith("US-"):
        variants.add("AC-" + ac_id[3:])
    variants.update(v.replace("-", "_").replace(".", "_") for v in list(variants))
    variants.update(v.replace("-", "").replace(".", "").replace("_", "") for v in list(variants))
    return sorted(variants)
```
For `"US-0001.2"` this produces (per the module's own self-check, line 930):
`["AC-0001.2", "AC00012", "AC_0001_2", "US-0001.2", "US00012", "US_0001_2"]`.

**`ac_ids_in_name` / `attributed_ac_ids` — location:** grepped
`def ac_ids_in_name|def attributed_ac_ids` across `agent/src/` — **both live in
`agent/src/test_results.py`, not in `ac_coverage_gate.py`.** `ac_coverage_gate.py` only calls them
via the `test_results` module import (`from .. import repo_files, stack_runner, tech_stack_signals,
test_results` at line 35), e.g. `test_results.ac_ids_in_name(line)` (line 211) and
`test_results.attributed_ac_ids(name)` (line 470).

Exact signatures, from `agent/src/test_results.py`:
```python
def attributed_ac_ids(test_name: str) -> tuple[list[str], str]:   # line 77
    """`(ids, mechanism)` where mechanism is 'canonical' | 'fallback' | 'none'. ..."""
```
```python
def ac_ids_in_name(test_name: str) -> list[str]:                  # line 105
    """Every AC id mentioned in a test name, normalised to `US-0001.2`. ..."""
```
`attributed_ac_ids` prefers a canonical `[US-0001.2]`-bracketed display name (via
`_CANONICAL_AC_RE = re.compile(r"\[(US-\d{4}\.\d+)\]")`, line 74) and falls back to the tolerant
`ac_ids_in_name` scan only when no canonical bracket is present.

---

## 3. `agent/src/graph.py` — `StageSpec`, `GraphState`

### `StageSpec`

Decorator + class header (lines 795–796): `@dataclass(frozen=True)` / `class StageSpec:`.

Complete field list, in declaration order, verbatim (lines 797–961):

```python
key: str
response_schema: (
    type[SpecificationDraftResponse]
    | type[PlanDraftResponse]
    | type[TechStackDraftResponse]
    | type[AcceptanceCriteriaTestsDraftResponse]
    | type[MinimalCodeToGreenDraftResponse]
    | type[AdversarialAuditDraftResponse]
    | type[ExitDraftResponse]
    | type[RemediationDraftResponse]
)
content_field: str | None
surface_tool_name: str
build_envelope: Callable[[dict[str, Any], list[str] | None], dict[str, Any]]
build_prompt: Callable[[GraphState], list[BaseMessage]]
max_cycles: int
render_markdown: Callable[[dict[str, Any]], str]

audit_response_schema: (
    type[SpecificationAuditResponse]
    | type[PlanAuditResponse]
    | type[MinimalCodeToGreenAuditResponse]
    | None
) = None
audit_content_field: str | None = None
build_audit_prompt: Callable[[GraphState], list[BaseMessage]] | None = None

requires_human_gate: bool = True
post_audit_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
post_approve_hook: Callable[[str, dict[str, Any], "GraphState", SandboxProvider], Awaitable[None]] | None = None
verify_fix_prompt: str | None = None
deterministic_verify: (
    Callable[[str, dict[str, Any], str, str | None, SandboxProvider, str], Awaitable[VerificationResult]] | None
) = None
max_verify_cycles: int = 3
hydrate_from_repo_file: Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
session_options: Callable[[GraphState, str], dict[str, Any]] | None = None
use_custom_agent: bool = False
prefill_from_repo_file: Callable[[str, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
build_interrupt_extra: Callable[["GraphState"], dict[str, Any]] | None = None
resolve_from_interrupt: Callable[[str, Any, "GraphState", SandboxProvider], Awaitable[dict[str, Any] | None]] | None = None
capture_baseline_commit: bool = False
sign_approval: bool = False
```

Confirms the field the prompt asked about: `deterministic_verify`'s `Callable` type is the
**6-argument** shape `(thread_id, revised content dict, run_id, baseline_commit, provider,
chat_provider) -> VerificationResult` (line 870), with the docstring (lines 872–888) explicitly
naming this as the Part-4/Task-5/Ruling-4 `chat_provider` addition. This is the current, post-change
shape — there is no separate 5-argument variant left anywhere.

**`hydrate_from_repo_file` — which stages wire it up.** Every `StageSpec(` instantiation in the
file (9 total: 8 in the `STAGES` list at lines 964–1223, plus `BROWNFIELD_BASELINE_SPEC` at
2471–2484) was read in full. Only **one** sets `hydrate_from_repo_file`:

```python
StageSpec(
    key="tech-stack",
    ...
    hydrate_from_repo_file=preflight_nodes.hydrate_tech_stack_from_repo_file,   # line 976
    prefill_from_repo_file=preflight_nodes.prefill_tech_stack_from_repo_file,   # line 977
    ...
)
```
`specification`, `plan`, `ac-to-tests`, `minimal-code-to-green`, `remediation`,
`adversarial-compliance`, `metrics-exit`, and `brownfield-baseline` (`BROWNFIELD_BASELINE_SPEC`) all
leave both `hydrate_from_repo_file` and `prefill_from_repo_file` unset (`None`, the dataclass
default).

**Correction to a likely stale assumption:** `preflight_nodes.hydrate_tech_stack_from_repo_file` is
qualified by module name in that assignment for a reason — the function is defined in
**`agent/src/preflight_nodes.py:397`**, not in `agent/src/app_discovery.py`. See §6 below; a design
doc that assumed this function lives in `app_discovery.py` is wrong about its current location.

What it actually does (`agent/src/preflight_nodes.py:397–420`, exact code):
```python
async def hydrate_tech_stack_from_repo_file(
    thread_id: str, state: "GraphState", provider: SandboxProvider
) -> dict[str, Any] | None:
    """StageSpec.hydrate_from_repo_file for the tech-stack stage: skip Copilot CLI drafting
    entirely and hydrate as pre-approved when tech-stack.approved.json already exists.
    ...
    """
    raw = await repo_files.read_repo_file(provider, thread_id, TECH_STACK_APPROVED_JSON_PATH)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(...)
        return None
```
i.e. the entire check is "does `.ai-dev-workflow/tech-stack.approved.json` exist and parse as JSON"
— presence+validity of that one file is the whole idempotency signal. `make_draft_node`
(`graph.py:1463–1486`) calls this before ever invoking the LLM, and on a non-`None` result marks the
stage `status="approved"` / `readiness=True` directly, bypassing audit and gate entirely, then still
fires `post_approve_hook` (line 1484) — the exact mechanism the field's own docstring describes.

**`GraphState`** — complete field list, verbatim, in declaration order (lines 159–268):
```python
class GraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    run_id: str
    provider: Literal["copilot", "claude"]
    manifest_exists: bool
    run_baseline_commit: str | None
    app_scan: dict[str, Any]
    brownfield_context: str
    raw_requirements_text: str
    requirements_attachments: list[dict[str, Any]]
    consumed_message_id: str | None
    stages: dict[str, StageState]
    rebuild: dict[str, Any]
    test_hardening: dict[str, Any]
    e2e: dict[str, Any] | None
    metrics_report: dict[str, Any]
    repo_scan: dict[str, Any]
    last_push: dict[str, Any] | None
    run_failure: dict[str, Any] | None
    token_usage_running: dict[str, Any] | None
```
That's 19 top-level keys. `provider: Literal["copilot", "claude"]` (line 207) is confirmed present,
exactly as the prompt expected from Part 4. **There is no `project_id`, `ticket_id`, or any
similarly-named field anywhere in `GraphState`** — the type was read in full, not sampled. The
closest existing "identity" concepts are `run_id` (re-minted per intake, i.e. per pipeline
attempt/resubmission on one thread — see its own long comment at lines 161–166) and the
`(owner, repo, source_branch)` triple that lives in `session_store.py`'s `sessions` table (§5), not
in `GraphState` at all. A LangGraph thread (`thread_id`) today == one session row == one
`GraphState` == one thing that gets a work branch; there is no state layer above that.

---

## 4. `agent/src/gates/write_scope_gate.py`

Full file read (335 lines). **`_WRITE_TOOL_NAMES` does not exist.** Grepped
`WRITE_TOOL_NAMES` (and `_WRITE_TOOL_NAMES`) across the **entire repository** at this ref — zero
matches anywhere, not just in this file. There is no tool-name-based allowlist/denylist constant of
any kind in the current write-scope enforcement.

What actually exists instead is a **path-pattern** classifier, not a tool-name classifier. The
module's real constants (lines 38–57):
```python
_DOTNET_TEST_PATTERNS = [
    r"(^|/)[A-Za-z0-9_.]+\.Tests(/|$)",
    r"(^|/)[A-Za-z0-9_.]+Tests\.csproj$",
    r"(^|/)[A-Za-z0-9_.]*Tests?\.cs$",
]
_TS_TEST_PATTERNS = [
    r"\.test\.tsx?$", r"\.spec\.tsx?$", r"(^|/)(tests|__tests__|test|e2e)/",
    r"(^|/)playwright\.config\.tsx?$", r"(^|/)vitest\.config\.tsx?$",
]
_PY_TEST_PATTERNS = [
    r"(^|/)test_[A-Za-z0-9_]+\.py$", r"(^|/)[A-Za-z0-9_]+_test\.py$",
    r"(^|/)tests?/", r"(^|/)conftest\.py$",
]
_ALL_PATTERNS = [re.compile(p) for p in _DOTNET_TEST_PATTERNS + _TS_TEST_PATTERNS + _PY_TEST_PATTERNS]
```
plus `_PIPELINE_OWNED_PREFIXES = (".ai-dev-workflow/", "APPROVALS.md", "AGENTS.md")` (line 69).
Enforcement (`check_write_scope`, lines 140–199) is a post-hoc `git diff --name-only <baseline>`
against these path patterns — never a tool restriction on the model's session.

**A tool-name-based "Layer 1" *did* exist and was explicitly removed.** The module's own docstring
(lines 8–15) says so directly:
> "There used to be a 'Layer 1' in front of it too: a `pre_tool_use_hook` (the old SDK-based Copilot
> session, wired via P4's `StageSpec.session_options`) that fast-failed an out-of-scope tool call
> before it ran. Neither current provider's CLI-exec turn has anything to translate that hook into
> -- ClaudeChatModel's and CopilotChatModel's own `_agenerate_inner` just log a warning and proceed
> when one is set -- so the wiring was removed from P4's StageSpec instead of firing that warning,
> for no enforcement benefit, on every single ac-to-tests turn."

Confirmed live in the actual chat-model code (`agent/src/claude_chat_model.py:294–300`):
```python
if self.pre_tool_use_hook is not None:
    logger.warning(
        "ClaudeChatModel.pre_tool_use_hook is set but Layer 1 write-scope enforcement has "
        "no CLI equivalent to translate it into -- Layer 2's git-diff gate "
        "(gates/write_scope_gate.py) is authoritative regardless, so this turn proceeds "
        "without it"
    )
```
(`copilot_chat_model.py:353–355` carries the identical pattern for the other provider.) The
`pre_tool_use_hook` parameter itself still exists in the plumbing (`chat_model.py`,
`claude_chat_model.py`, `copilot_chat_model.py`) but is a documented no-op today — it is never set
by any current `StageSpec.session_options` in `graph.py` (grepped; none of the 9 stages' inline
`session_options` lambdas pass it).

**Is a delete/remove-file tool name present anywhere?** No — and the gate's own design leans on
that absence. Quote (lines 164–168):
> "Deterministic remediation instead of feedback the model cannot act on: the draft session has
> create/edit tools but NO delete and NO bash, so 'revert these files' deadlocked the stage at the
> verify cap..."

Checking every stage's inline tool list in `graph.py` confirms this: ac-to-tests' draft tools are
`["builtin:view", "builtin:grep", "builtin:glob", "builtin:edit", "builtin:create",
"builtin:apply_patch", "builtin:skill"]` (graph.py:1074–1077) — no delete/remove tool. Because
no delete tool exists, `check_write_scope` does its own filesystem revert (`git checkout --` for
tracked files, `rm -rf --` for untracked ones, run directly via `provider.exec_in_sandbox`, lines
179–190) rather than asking the model to clean up after itself, quarantining a copy first under
`.ai-dev-workflow/quarantine/<run_id>/` in case the "violation" was actually a legitimate test in an
unrecognized language.

---

## 5. `agent/src/session_store.py`

**Schema.** Table `dbo.sessions` (SQL Server, via `aioodbc`). Full column list, exact quote
(`session_store.py:187–191`):
```python
_COLUMNS = [
    "session_id", "owner", "repo", "user_login", "title", "source_branch", "work_branch",
    "run_id", "current_stage", "status", "started_at", "ended_at", "merge_ready",
    "pr_title", "pr_url", "failure_stage", "failure_type", "failure_message", "updated_at",
]
```
Cross-checked against the actual DDL, `agent/db/migrations/0001_create_sessions.sql:3–26`:
```sql
CREATE TABLE dbo.sessions (
    session_id       UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,   -- == LangGraph thread_id == sandbox session_id
    owner            NVARCHAR(255)    NOT NULL,
    repo             NVARCHAR(255)    NOT NULL,
    user_login       NVARCHAR(255)    NOT NULL,
    title            NVARCHAR(200)    NOT NULL,
    source_branch    NVARCHAR(500)    NOT NULL,               -- PR-target branch chosen at start
    work_branch      NVARCHAR(500)    NOT NULL,               -- ai-dev-workflow/<session_id>, computed once by branch_naming.py, stored, never recomputed
    run_id           VARCHAR(8)       NULL,
    current_stage    NVARCHAR(100)    NULL,
    status           VARCHAR(20)      NOT NULL
                       CONSTRAINT CK_sessions_status
                       CHECK (status IN ('in_progress','completed','failed','rejected')),
    started_at       DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME(),
    ended_at         DATETIME2(0)     NULL,
    merge_ready      BIT              NULL,
    pr_title         NVARCHAR(500)    NULL,
    pr_url           NVARCHAR(500)    NULL,
    failure_stage    NVARCHAR(100)    NULL,
    failure_type     NVARCHAR(100)    NULL,
    failure_message  NVARCHAR(1000)   NULL,
    updated_at       DATETIME2(0)     NOT NULL DEFAULT SYSUTCDATETIME()
);
CREATE INDEX IX_sessions_repo_recent ON dbo.sessions(owner, repo, source_branch, started_at DESC);
```
One row per session; `session_id` (a GUID, == the LangGraph `thread_id` == sandbox session id) is
the primary key. **No `project_id`/`ticket_id`/parent-grouping column exists** — the only grouping
key available today is the `(owner, repo, source_branch)` triple the index is built on, and that
identifies a *branch*, not a project or ticket.

**Every public function signature** (all `async`, all in `session_store.py`):
```python
async def create_session(
    session_id: str, *, owner: str, repo: str, user_login: str,
    source_branch: str, work_branch: str, title: str,
) -> None:                                                            # line 56
async def touch_run(session_id: str, *, run_id: str, title: str | None) -> None:   # line 89
async def update_current_stage(session_id: str, stage_key: str) -> None:          # line 128
async def close_session(
    session_id: str, *, run_id: str | None, status: str,
    failure: dict[str, Any] | None = None, merge_ready: bool | None = None,
    pr_title: str | None = None, pr_url: str | None = None,
) -> None:                                                            # line 141
async def delete_session(session_id: str) -> None:                    # line 178
async def get_session(session_id: str) -> dict[str, Any] | None:      # line 194
async def list_sessions(
    owner: str, repo: str, source_branch: str | None = None, limit: int = _DEFAULT_LIST_LIMIT
) -> list[dict[str, Any]]:                                            # line 202
```
`list_sessions` is scoped by `(owner, repo[, source_branch])` — there is no way to list sessions by
any grouping other than the repo (optionally narrowed to one branch) today.

**Migrations directory**, filenames in order (`agent/db/migrations/`):
```
0001_create_sessions.sql
0002_create_repo_vaults.sql
0003_create_org_settings.sql
```
Next free migration number: **`0004`**.

---

## 6. `agent/src/app_discovery.py` (+ correction: `preflight_nodes.py`)

**`hydrate_tech_stack_from_repo_file` is not in this file.** Grepped
`def hydrate_tech_stack_from_repo_file` across `agent/src/` — the only match is
`agent/src/preflight_nodes.py:397`. `app_discovery.py` (417 lines, read in full) has no function by
that name or an equivalent; its own module docstring (lines 1–18) describes a narrower, unrelated
job: "find candidate app marker files and turn them into `DiscoveredApp` records, purely by
regex/JSON inspection, no model involved." Its exported surface is `fingerprint`,
`classify_candidates`, `load_stack_catalog`, `collect_evidence`, `app_discovery_pre_node`,
`candidates_to_apps`, `app_check_record_node` — none of these are a stage-hydration hook. A design
doc that placed `hydrate_tech_stack_from_repo_file` in `app_discovery.py` has the wrong file for it;
see §3's quote of the real function in `preflight_nodes.py`.

**Brownfield-baseline's actual trigger condition** likewise lives in `preflight_nodes.py`, in
`scaffold_node` (not in `app_discovery.py`), gated on the presence of `.ai-dev-workflow/manifest.json`
(`MANIFEST_PATH`, `preflight_nodes.py:42`). Exact quote (`preflight_nodes.py:225–229`):
```python
    # manifest.json absence is the canonical "never onboarded before" signal -- gates whether
    # build_graph()'s conditional edge routes into brownfield-baseline's brownfield sub-flow. Read once, here, and
    # routed on from state later: app discovery writes to this file mid-run, so a fresh read at
    # the branch point would always report "onboarded".
    manifest_exists = await repo_files.read_repo_file(provider, thread_id, MANIFEST_PATH) is not None
```
The actual routing decision is `graph.py`'s `_route_after_tech_stack` (`graph.py:2390–2403`):
```python
def _route_after_tech_stack(state: GraphState) -> str:
    if state.get("manifest_exists", True):
        return "next"
    if tech_stack_signals.is_greenfield_repo(state):
        return "brownfield_write_manifest"
    return "brownfield_baseline_pre"
```
So: manifest present → skip brownfield entirely (`"next"`); manifest absent + repo scan found no
app candidates (greenfield) → skip straight to deterministic ratification
(`brownfield_write_manifest_node`, which just flips `manifest.json` to `{"onboarded": true, ...}`,
`preflight_nodes.py:385–394`); manifest absent + something was found (real brownfield) → run the LLM
draft stage (`brownfield_baseline_pre` → `BROWNFIELD_BASELINE_SPEC`). This condition is a pure
file-existence check against the target repo, nothing content-based — "already cached" and
"onboarded" are the same bit today (there's no separate freshness/fingerprint check gating
brownfield re-baselining the way tech-stack's own `hydrate_tech_stack_from_repo_file` at least
validates JSON before trusting the cache).

**`load_stack_catalog`** (`app_discovery.py:221–233`):
```python
@lru_cache(maxsize=None)
def load_stack_catalog() -> list[dict[str, Any]]:
    """The 8 canned monorepo stacks the Tech Stack tab's dropdown offers, one markdown file per
    stack under templates/tech_stacks/ -- DATA the user picks from and edits, not a prompt..."""
```
Shape: `list[dict]`, each `{"id": <filename stem>, "title": <file's first "# " heading>, "markdown":
<full file text verbatim>}`, sourced from `agent/src/templates/tech_stacks/*.md`. The self-check
(`app_discovery.py:404–407`) pins the exact 8 stack ids: `angular-dotnet`, `react-dotnet`,
`nextjs-dotnet`, `nextjs-flask`, `nextjs-fastapi`, `react-express`, `blazor-dotnet`, `vue-dotnet`.
Cached process-wide via `@lru_cache` — one process never sees a stack file edit without a restart.

---

## 7. Frontend, `src/app/`

**`ticket` / `board` / `kanban`:** grepped case-insensitively across all of `src/` at this ref —
**zero matches for any of the three terms.** No ticket/issue/board/kanban UI exists anywhere in the
frontend today.

**"project" as a concept distinct from "repo":** grepped `project` (case-insensitive) across
`src/app/` specifically — **zero matches.** The word does not appear anywhere in the app router
tree. `repo == project` today is not just an inference from missing UI; the string "project" is
simply absent from the relevant part of the frontend entirely.

**`/select`** (`src/app/(boxed)/select/page.tsx`, 320 lines, read in full) is a two-pane repo/branch
picker: left pane lists the user's GitHub repos (`GET /api/github/repos`, backed by the user's
GitHub OAuth connection), right pane lists that repo's branches
(`GET /api/github/branches?owner=...&repo=...`) and, once a branch is chosen, an onboarding-status
badge (`GET /api/github/onboarding-status`) plus `<SessionHistory>` scoped to
`(owner, repo, sourceBranch)`. Its own comment states the model directly (lines 250–251):
> "Sessions are branch-scoped now (each gets its own `ai-dev-workflow/<session_id>` branch), so the
> list is too -- shown once both repo and branch are chosen, keyed the same way."

Starting a session mints a client-side UUID and navigates, nothing more
(`RepoBranchSection.startNewSession`, lines 206–210):
```tsx
  function startNewSession() {
    if (!selectedBranch) return;
    const sessionId = crypto.randomUUID();
    router.push(`/workflow/${repo.owner}/${repo.repo}/${sessionId}/${selectedBranch}`);
  }
```
There is no intermediate "project" selection or creation step anywhere in this flow — a GitHub repo
IS the top-level unit you pick, full stop.

**Session-provisioning frontend call.** The actual `fetch` call to the provisioning route is in
`src/components/SandboxSessionBoot.tsx:40–44` (not in `/select`'s own page code — it fires from the
workflow page once a session is opened, as a background non-blocking boot):
```tsx
    fetch("/api/sessions/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, owner, repo, branch, resume: Boolean(resume) }),
    })
```
That Next.js route (`src/app/api/sessions/provision/route.ts`, 66 lines, read in full) is a
server-to-server proxy: it reads the caller's GitHub access token and a fresh Entra access token
server-side, then forwards to the agent's own endpoint —
```ts
  const response = await agentFetch("sessions/provision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      thread_id: sessionId, owner, repo, branch,
      github_token: accessToken,
      user_login: userLogin ?? "",
      resume: Boolean(resume),
      entra_assertion: token?.entraAccessToken ?? null,
    }),
  });
```
Its own comment (lines 11–17) confirms there is no project-level session key: *"There is no more
deterministic (owner, repo, user) -> session id formula ... Concurrency is fully open: any number of
sessions can be in-progress on the same repo at once, each on its own branch."* — i.e. today's model
is explicitly many-sessions-per-repo already (good news for a project/ticket model layering on top:
concurrent independent runs against one repo already work), just with zero grouping/labeling of
those sessions beyond the branch they're on.

---

## 8. `agent/config/models.yaml`

It already has a provider dimension — **not** a flat model-id-per-stage. Schema is
`{stage}: {copilot: {draft_model, [audit_model], [fix_model]}, claude: {draft_model, [audit_model],
[fix_model]}}` per the file's own header comment (lines 1–6): *"Every stage nests a full model
config per AGENT_PROVIDER value -- `{stage}: {copilot: {draft_model, audit_model}, claude:
{draft_model, audit_model}}`."*

One real stage block, verbatim (`models.yaml:34–40`):
```yaml
tech-stack:
  # Also used for the "extract" role (Tech Stack tab's Submit-time JSON extraction) -- shares
  # draft_model, see model_config.get_model_name's own comment on why that's not a config gap.
  copilot:
    draft_model: gpt-5.4-mini
  claude:
    draft_model: haiku
```
and one with both `audit_model` and provider-specific comments (`models.yaml:41–47`):
```yaml
specification:
  copilot:
    draft_model: gpt-5.4-mini
    audit_model: gemini-3.6-flash
  claude:
    draft_model: haiku
    audit_model: sonnet
```
This confirms whatever design-doc assumption exists about needing to *add* a provider dimension to
this file is already stale — it was added in Part 1 (the file's own comment attributes the `claude:`
blocks to "Task 10 (part-1-provider-unification)"). Whatever Part 3 needs from `models.yaml` would
be a **third** dimension (e.g. per-project override) layered on top of an already-two-dimensional
`{stage: {provider: {...}}}` shape, not a first one.

---

## 9. `agent/src/git_ops.py`

Full file read (661 lines). Its own docstring is explicit about scope (lines 1–5): *"Git operations
against a sandbox's own clone ... There is no local working tree on the agent's own host -- every
operation here runs inside the per-session sandbox via `SandboxProvider.exec_in_sandbox`."* — this
module never itself clones anything; it only operates on a clone that already exists inside a
running sandbox container.

Function inventory (name — one-line purpose):
- `set_push_token(thread_id, token) -> None` — caches this thread's push credential in memory (line 42).
- `get_push_token(thread_id) -> str | None` — retrieves it; reused by PR creation so it doesn't need a second copy of the token threaded through (line 51).
- `get_last_push(thread_id) -> dict | None` — last push outcome, feeds the frontend's push-failure warning chip (line 47).
- `async open_pull_request(*, owner, repo, source_branch, work_branch, title, body, token) -> str | None` — opens a GitHub PR via plain REST `POST /repos/{owner}/{repo}/pulls`; idempotent against a 422 "already exists" by looking up and returning the existing PR's URL (lines 57–102).
- `async delete_remote_branch(*, owner, repo, branch, token) -> bool` — deletes a branch ref via REST `DELETE .../git/refs/heads/{branch}`; treats 404 as success (lines 105–134).
- `async push_head(provider, thread_id) -> None` — force-pushes the session's own work-branch HEAD to `origin`, using a one-shot credential-helper file inside the container (lines 137–176).
- `async record_run_failure(thread_id, payload, run_id=None) -> dict` — durably records a terminal failure (ledger entry + `session_store.close_session` + a commit) (lines 179–207).
- `is_empty_commit_output(combined_output: str) -> bool` — pure classifier for git's various "nothing to commit" phrasings (lines 229–232).
- `async commit_paths(provider, thread_id, paths: list[str], message: str) -> None` — stage+commit exactly the given repo-relative paths, then push; the general-purpose commit primitive (lines 235–276).
- `async commit_ai_dev_workflow(provider, thread_id, message: str) -> None` — thin wrapper of `commit_paths` scoped to `.ai-dev-workflow/` only (lines 279–282).
- `generated_ignore_entries(untracked, tracked) -> list[str]` — pure; derives `.gitignore` entries for build/output paths that appeared but were never authored (lines 364–438).
- `async ignore_generated_files(provider, thread_id) -> list[str]` — appends the entries above to `.gitignore` (lines 441–474).
- `async ensure_gitignore(provider, thread_id) -> list[str]` — writes the static baseline `.gitignore` once per thread and untracks anything it newly matches (lines 477–519).
- `async commit_all(provider, thread_id, message: str) -> None` — `git add -A` + commit + push; the "everything a code-writing stage touched" commit (lines 522–552).

**What a "Connect a Repository" flow would reuse:** none of the above — the clone itself happens
**outside** `git_ops.py` entirely. Tracing where `REPO_CLONE_URL` actually gets used: `sessions_api.py`
builds it from user input (`agent/src/sessions_api.py:120`, `repo_clone_url =
f"https://github.com/{body.owner}/{body.repo}.git"`), passes it into the sandbox provider's
`start(...)` call (`agent/src/sandbox/provider.py:67`, `agent/src/sandbox/local_docker.py:162` /
`azure_aci.py:148`), which injects it as a container env var
(`local_docker.py:282`/`azure_aci.py:231`, `f"REPO_CLONE_URL={repo_clone_url}"`), and the actual
`git clone` command runs inside `agent/sandbox-image/entrypoint.sh` (lines 86–88 and 155–158,
`clone --branch "$REPO_BRANCH" --single-branch "$REPO_CLONE_URL" "$WORKSPACE_DIR"`) when the
container boots. So a "Connect a Repository" flow for an **existing** GitHub repo the user already
has push access to is already fully solved end-to-end by the current provisioning path — reusing it
just means calling the existing `sessions/provision` flow with a real `owner`/`repo`/`branch`,
exactly like `/select` already does.

**What's missing for scaffolding a brand-new empty repo from scratch:** grepped `git init` across
`agent/src/` and `agent/sandbox-image/` — **zero matches, genuinely absent.** Grepped for any
"create a GitHub repo" REST call (`create.*repo`, case-insensitive) across `agent/src/` — the only
hit is an unrelated markdown instruction inside a tech-stack template file
(`agent/src/templates/tech_stacks/blazor-dotnet.md:39`, "Create a root `.gitignore`"), not code.
`entrypoint.sh` does already have one relevant escape hatch — line 175, `echo "entrypoint:
REPO_CLONE_URL not set -- skipping clone, starting a bare sandbox"` — a bare/no-clone sandbox mode
already exists, but it is for "no repo at all" (nothing to boot against), not for "create and clone
a fresh empty GitHub repo." There is no code path today that calls GitHub's create-repository API,
and no `git init`-based scaffold-from-nothing path either — both would be new work for a
"Connect a Repository" → "or start a brand-new one" flow.

`agent/src/branch_naming.py` (25 lines, read in full) is the one place the per-session branch name
format lives — `work_branch_for(session_id: str) -> str` returns `f"ai-dev-workflow/{session_id}"`
— computed once at provision time and stored in `dbo.sessions.work_branch`, never recomputed by any
other consumer (its own docstring names `git_ops.py`, `entrypoint.sh`'s `WORK_BRANCH` env var, and
the frontend's `GET /sessions/{id}` as the readers).
