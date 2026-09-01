# Part 2 UI backend research notes: current-state ground truth

**Methodology note:** every citation below is against the real, current on-disk content of
`D:\Projects\bancroftway\ai-dev-workflow` at the time this was written — `git rev-parse HEAD` =
`88f955e73956dc0636a952d757ecb1c6638e7404`, `git rev-parse --abbrev-ref HEAD` =
`feature/claude-support`, tree clean (`git status --porcelain` empty). This is worth flagging
explicitly: the branch name and clean-tree status differ from the `feature/react-langgraph`
mega-branch with a long `M`/`??` diff described elsewhere for this effort. Nothing below is
affected by that discrepancy — every finding was produced by directly reading the files at their
absolute paths (not `git show`), and the files present are exactly the Part 1
(provider-unification: `chat_model.py`/`claude_chat_model.py`/`copilot_chat_model.py`/
`cli_agent_exec.py`/`structured_output.py`) and Part 3 (ticket/project model: `project_store.py`,
`dbo.projects`, `dbo.sessions.project_id`/`awaiting_gate`) content the task brief describes as
already built — so this checkout is the right state of the world to research against. Flagging the
branch-name mismatch only so nobody re-derives a false "these two branches disagree" alarm from it
later.

All file:line citations are against the files as read; nothing was edited.

---

## 1. The event schema — no unification exists

Read `agent/src/chat_model.py`, `agent/src/claude_chat_model.py`, `agent/src/copilot_chat_model.py`,
`agent/src/cli_agent_exec.py`, and (since `chat_model.py` re-exports from it) `agent/src/structured_output.py`.

**There is no normalized/unified event shape or vocabulary anywhere in this backend today.** No
enum, no dataclass, nothing named `SessionEventType` or similar. Confirmed by grep across
`agent/src` for `SessionEventType|class.*Event\b|EventType|StreamEvent` — the only hits are two
docstring mentions of a type that used to exist and is now gone:

- `agent/src/copilot_chat_model.py:5-8` (module docstring): "This replaces the previous SDK-based
  implementation, which held a persistent TCP-connected CopilotClient/CopilotSession pair open...
  session.send(), a **SessionEventType event stream**, on_permission_request callbacks). None of
  that exists anymore."
- `agent/src/copilot_chat_model.py:244`: a comment noting the old SDK's `ASSISTANT_USAGE` event is
  also gone.

So a real, typed per-event vocabulary existed in the *previous* (pre-Part-1) SDK-server
implementation, and was deliberately deleted as part of the CLI-exec rewrite. **Any event-log UI
built on top of the current backend has nothing to consume at that granularity — this has to be
designed from scratch, not "wired up to" something already there.**

What actually happens today, per provider, inside each `BaseChatModel._agenerate_inner`:

- **Claude** (`claude_chat_model.py:341-482`): execs `claude -p --output-format json [--resume
  <id>] ...` via `cli_agent_exec.run_turn`, waits for the whole turn to finish, then parses the
  **single terminal JSON object** (`json.loads(result.stdout)`, line 454) for `result`, `is_error`,
  `usage`, `total_cost_usd`, `session_id`. Everything about the turn — every tool call, every
  intermediate delta — lives only inside that one already-finished process's stdout; nothing
  intermediate is ever surfaced to this process while the turn is running.
- **Copilot** (`copilot_chat_model.py:284-547`): execs `copilot -p ...` the same way, but the CLI
  emits JSONL (one object per line). `_parse_copilot_jsonl` (lines 110-143) parses every line into
  a plain `list[dict]`, but `_agenerate_inner` only ever reads the **last** element (`events[-1]`,
  line 523, "best-effort guess, NOT confirmed against real output" per the module's own docstring)
  for the same `result`/`is_error`/`usage`/`total_cost_usd` fields. The other lines (deltas,
  presumably tool-call events) are parsed, held in a local list, and then discarded when the
  function returns — never returned to the caller, never logged anywhere structured.

Both providers collapse everything to one `ChatResult`/`AIMessage` plus a private `_last_usage`
dict of exactly this shape (identical in both files, e.g. `claude_chat_model.py:471-480`):

```python
usage = parsed.get("usage") or {}
self._last_usage = {
    "model": self.model_name or "default",
    "input_tokens": usage.get("input_tokens", 0),
    "output_tokens": usage.get("output_tokens", 0),
    "cost": parsed.get("total_cost_usd"),
}
```

Both classes' own comments note `reasoning_tokens`/`cache_read_tokens`/`cache_write_tokens` are
deliberately NOT modeled (claude_chat_model.py:299-303, copilot_chat_model.py:242-246) because
fabricating a `0` would misrepresent "not reported" as "measured zero."

The one place any provider's own transcript is read back at all is
`claude_chat_model.read_skill_invocations` (lines 594-629): it `cat`s Claude's own JSONL project
transcript (`_CLAUDE_PROJECTS_DIR = "/home/vscode/.claude/projects/-workspace-repo"`, line 82)
inside the sandbox and scans `assistant`-role lines for `tool_use` blocks named `"Skill"` — but
only to answer one yes/no question ("did this session invoke skill X") for `gates/skill_gate.py`,
fails open (`None`) on any read problem, and is never used for anything display-oriented.
Copilot's equivalent (`copilot_chat_model.py:657-674`) is **unconditionally `None`** — the
persistent-server transcript it used to read (`~/.copilot/session-state/<id>/events.jsonl`) no
longer exists under the CLI-exec model, and nothing has replaced it. **So even the one piece of
raw-transcript reading that does exist today only works for one of the two providers.**

## 2. Persistence — no per-turn event log anywhere durable

Read `agent/src/session_store.py`, `agent/src/db.py`, all five files in `agent/db/migrations/`, and
(having found it referenced from `metrics_nodes.py`) `agent/src/repo_files.py`.

`dbo.sessions` (`agent/db/migrations/0001_create_sessions.sql`, extended by `0004`/`0005`) and
`dbo.projects` hold only coarse, mutate-in-place session/project *state* — never an event/turn
history. `session_store._COLUMNS` (`session_store.py:232-237`):

```python
_COLUMNS = [
    "session_id", "owner", "repo", "user_login", "title", "source_branch", "work_branch",
    "run_id", "current_stage", "status", "started_at", "ended_at", "merge_ready",
    "pr_title", "pr_url", "failure_stage", "failure_type", "failure_message", "updated_at",
    "project_id", "awaiting_gate",
]
```

Nothing log/transcript/event-shaped exists in any of the 5 migration files. **If nothing else
changes, there is no DB-backed history of what happened turn-by-turn inside a run — only whatever
single current snapshot `dbo.sessions` holds.**

The *closest* thing to a per-turn event log is `agent/src/repo_files.py`'s workflow action ledger,
`.ai-dev-workflow/ledger.jsonl` (`LEDGER_PATH`, line 28), which is real and is written today:

```python
async def append_ledger_entry(provider: SandboxProvider, thread_id: str, entry: dict[str, Any]) -> None:
    ...
    payload = {"timestamp": time.time(), **entry}
```
(`repo_files.py:109,119`) — every entry does get a wall-clock timestamp. `graph.py`'s draft node
appends one entry per LLM turn, e.g. `graph.py:1830-1834`:
```python
await repo_files.append_ledger_entry(
    get_sandbox_provider(), thread_id,
    {"stage": stage_spec.key, "node": "draft", "readiness": response.readiness, "token_usage": model._last_usage},
)
```

But this is **not** a durable, queryable event log in the sense the redesign wants:
- It lives inside the disposable sandbox container's own git working tree (read/written via shell
  `exec_in_sandbox`, not the DB) — not reachable by any API route today.
- It is reset to empty at the start of every fresh run (`reset_ledger`, `repo_files.py:99-106`,
  called once by `scaffold_node`) and is **never git-committed**: grepping every call site of
  `git_ops.commit_paths` across `agent/src` shows only `spec_ledger.LEDGER_PATH`
  (`.ai-dev-workflow/spec/ledger.json`, a *different* file — the US/AC id registry) ever gets
  committed (`test_hardening_nodes.py:304`). `repo_files.LEDGER_PATH` is committed nowhere.
  **Once a sandbox container is torn down (idle reap, explicit stop, session delete), this ledger
  is gone permanently** — the only trace that survives is whatever aggregate `metrics_nodes.py`
  managed to sum from it before that happened (see item 5).
- Its granularity is one line per *node* (an entire LLM turn, or a deterministic node), not per
  tool call — there is no raw stdout/stderr, no diff, no individual Read/Edit/Bash-call record in
  it. It could not by itself back a "folding tool-call rows" view.

## 3. The AG-UI / LangGraph bridge — real and mounted, with a Next.js hop in between

`ag_ui_langgraph` **is** imported and mounted, live, in `agent/main.py`:

```python
# agent/main.py:12-13
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
...
# agent/main.py:22, 44-48
from src.graph import graph
...
add_langgraph_fastapi_endpoint(
    app=app,
    agent=LangGraphAGUIAgent(name="workflow", graph=graph),
    path="/",
)
```

`graph` is `graph.py`'s own compiled `StateGraph` — this wraps that exact graph directly, no
intermediate translation layer. It is mounted at `path="/"`, i.e. the **bare root** of the same
FastAPI app that also serves `/sessions`, `/projects`, `/vault-config`, `/org-settings` (all via
routers included earlier in the same file, `main.py:38-42`). The dependency is real, not
aspirational: `agent/pyproject.toml:8,12` lists `"ag-ui-langgraph>=0.0.42"` and
`"copilotkit>=0.1.94"`.

The bridge is not called directly by the browser, though. `src/app/api/copilotkit/[[...slug]]/route.ts`
(Next.js) runs a `CopilotRuntime` (`@copilotkit/runtime/v2`) configured with a `LangGraphHttpAgent`
pointed at the FastAPI app's root:

```ts
// src/app/api/copilotkit/[[...slug]]/route.ts:16-32
const AGENT_URL = process.env.AGENT_URL ?? "http://localhost:8123/";
const workflowAgent = new LangGraphHttpAgent({ url: AGENT_URL });
workflowAgent.use(new A2UIMiddleware({ defaultCatalogId: CATALOG_ID }));
const runtime = new CopilotRuntime({ agents: { workflow: workflowAgent } });
```

So the real chain is: React (`@copilotkit/react-core`) → Next.js `/api/copilotkit` (a
`CopilotRuntime` in `"single-route"` mode) → FastAPI `/` (`ag_ui_langgraph`'s
`add_langgraph_fastapi_endpoint`, wrapping `graph.py`'s compiled graph directly). This matches the
plan's assumption, with one nuance worth being explicit about: there is a full Next.js server hop
in the middle, not a direct browser→FastAPI SSE connection — any new event-log/diff/gate UI still
has to go through (or around) that Next.js route, not just talk to port 8123 directly. Also worth
noting as precedent: that same route already applies `A2UIMiddleware` to intercept the same AG-UI
event stream for an unrelated purpose (turning `a2ui_operations` tool-result envelopes into custom
generative-UI surface events) — proof that scanning/re-interpreting this stream for a
purpose CopilotKit's own UI doesn't natively support is an already-used pattern here, not a novel
risk.

## 4. The Gate / interrupt mechanism

`agent/src/graph.py`'s `StageSpec.requires_human_gate` defaults to `True` (`graph.py:952-955`), but
only **3 of the 8 stages** actually pause: `tech-stack` sets it explicitly `True`
(`graph.py:1112`); `specification` and `plan` never override it, so they inherit the `True`
default; `ac-to-tests` (1189), `minimal-code-to-green` (1262), `remediation` (1302),
`adversarial-compliance` (1331), and `metrics-exit` (1358) all explicitly set it `False`. Those 5
are gated only by `deterministic_verify` checks or unconditional auto-approval — they never
present a human approve/reject decision at all.

`make_gate_node`/`gate_node` (`graph.py:2370-2431`) is where the pause happens:

```python
# graph.py:2371-2403
async def gate_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    thread_id = config["configurable"]["thread_id"]
    stage = state["stages"][stage_spec.key]
    # Pauses here (BR-4/Section 6 Gate) until the frontend's useInterrupt
    # resolve(payload) resumes this exact node with that payload -- unless this stage is
    # supporting infrastructure with no tab to review it in (requires_human_gate=False), in
    # which case it proceeds straight through to the same approved-marking body every other
    # stage already runs post-interrupt-resolve. ...
    resume_value: Any = None
    if stage_spec.requires_human_gate:
        extra = stage_spec.build_interrupt_extra(state) if stage_spec.build_interrupt_extra else {}
        if sandbox_registry.get(thread_id) is not None:
            try:
                await session_store.set_awaiting_gate(thread_id, True)
            except Exception:
                logger.warning(...)
        resume_value = interrupt({"stage": stage_spec.key, "draft": stage["draft"], **extra})
```

This is a plain LangGraph `interrupt()` call — the payload is `{"stage": ..., "draft": ...}` plus
whatever `StageSpec.build_interrupt_extra` adds. The comment **in the backend's own code** states
the resume contract explicitly: it is resumed by "the frontend's `useInterrupt` `resolve(payload)`"
— i.e. CopilotKit's own interrupt-hook family, talking through the chain described in item 3, not
through any bespoke REST call.

**There is no approve/reject REST endpoint in `agent/src/sessions_api.py`.** The entire file
(1029 lines) was read/grepped for `gate|approve|reject`; the only action endpoint that exists is:

```python
# sessions_api.py:396-401
class SessionActionRequest(BaseModel):
    """Named actions only -- the frontend never sends shell. Adding an action = a new Literal
    member plus a handler branch below; anything else is rejected by validation before it runs."""
    action: Literal["refresh-secrets"]
    entra_assertion: str = ""
```

`"refresh-secrets"` is the only member of that `Literal` today. Resolving a gate — actually
sending the human's approve/reject/edit decision back into the paused graph — happens **entirely**
through the LangGraph `interrupt()`/AG-UI resume protocol at the root-mounted `ag_ui_langgraph`
endpoint (item 3), never through `sessions_api.py`.

The only way an external REST caller can observe "this session is currently paused at a gate"
today is the durable `awaiting_gate` boolean, set immediately before `interrupt()` fires
(`session_store.set_awaiting_gate`, called at `graph.py:2396-2402`) and cleared by
`update_current_stage` once any approval path resolves (`session_store.py:144-163`). It is
surfaced read-only via `GET /sessions` / `GET /sessions/{id}` → `SessionResponse.awaiting_gate`
(`sessions_api.py:299`) — this exists purely so the session-list/board UI can show a paused
indicator by polling; it is not itself a mechanism for resolving anything.

Separately, `agent/src/approvals.py` (`record_approval`/`latest_approval`) is **not** the
approve/reject transport either — it is an audit-trail side effect, called from `gate_node` only
*after* a gate has already resolved (`graph.py:2419-2426`, gated on `StageSpec.sign_approval`),
appending a content-hash-signed row to a git-committed `APPROVALS.md` file in the repo. Different
concern entirely (tamper-evidence for `specification`/`plan`'s approved content), not part of how
approval is delivered.

## 5. Usage/cost tracking — real, but scattered across three disconnected layers

- **Layer 1 — in-process, per-turn, ephemeral:** each `ChatModel`'s private `_last_usage` dict
  (item 1) — overwritten on every call, never persisted, never exposed by any API.
- **Layer 2 — OTEL spans, drops cost:** `_agenerate` in both provider modules
  (`claude_chat_model.py:314-339`, identical shape in `copilot_chat_model.py:257-282`) attaches
  only two numbers and a model name to the span — **not cost**:
  ```python
  if self._last_usage is not None:
      llm_span.set_attribute("gen_ai.usage.input_tokens", self._last_usage["input_tokens"])
      llm_span.set_attribute("gen_ai.usage.output_tokens", self._last_usage["output_tokens"])
      llm_span.set_attribute("gen_ai.response.model", self._last_usage["model"])
  ```
  `self._last_usage["cost"]` is computed but never attached to anything here. `telemetry.py`'s
  `setup()` (lines 39-73) sends spans to the console by default locally and only ships them
  anywhere queryable (OTLP/Azure Monitor) if `OTEL_EXPORTER_OTLP_ENDPOINT` is set — so in an
  ordinary local/dev run, even this partial signal isn't captured anywhere durable either.
- **Layer 3 — the ledger, summed only once, at the very end:** `graph.py`'s draft/audit nodes write
  `token_usage: model._last_usage` into `.ai-dev-workflow/ledger.jsonl` per turn
  (`graph.py:1830-1834` and `:1942`). `metrics_nodes._sum_token_usage`
  (`agent/src/metrics_nodes.py:232-254`) is the **only place anything sums these into a total**,
  and it only runs once, as part of the `metrics-exit` stage:
  ```python
  async def _sum_token_usage(provider: Any, thread_id: str) -> dict[str, Any]:
      raw = await repo_files.read_repo_file(provider, thread_id, repo_files.LEDGER_PATH)
      totals = {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost": 0.0, "by_stage": {}}
      ...
      for key, total_key in (("input_tokens", "total_input_tokens"), ("output_tokens", "total_output_tokens")):
          ...
      cost = usage.get("cost") or 0.0
      totals["total_cost"] += cost
      by_stage["cost"] += cost
      return totals
  ```
  This becomes the `token_usage_summary` embedded in the Metrics Exit report (shape confirmed via
  `exit_nodes.py`'s own test fixture, line 693: `"token_usage_summary": {"total_input_tokens": 100,
  "total_output_tokens": 50, "total_cost": 0.01}`).

**There is no one canonical, live source of usage numbers.** Per-run totals only exist after a run
reaches Metrics Exit (and only if the sandbox/ledger still exists then, per item 2); nothing
aggregates across runs to a per-project number at all; nothing is queryable mid-run. A "cost/token
display" in the new UI has no existing API to call — it would need either a new endpoint that
reads the live ledger out of a running sandbox, or a new persisted running total, neither of which
exists today.

## 6. Secret redaction — three separate, non-overlapping mechanisms

The brief's pointer to `e2e_nodes.py` "secret-stripping" turned out to be real but a different
concern than it sounds like. There are three distinct redaction mechanisms in this codebase, and
none of them operates on anything resembling a persisted tool-output event log (because none
exists, per item 1):

1. **`e2e_nodes.py:_boot_process` (lines 654-681)** — protects the *end-user application under
   test*, not the coding agent's own output:
   ```python
   f"env -u COPILOT_SDK_AUTH_TOKEN -u COPILOT_CONNECTION_TOKEN -u COPILOT_GITHUB_TOKEN -u GITHUB_TOKEN -u ANTHROPIC_API_KEY "
   f"{_DEBUG_BOOT_ENV} "
   f"setsid nohup sh -c {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 & "
   ```
   The docstring (lines 664-667) explains why: "`docker exec` inherits this container's own env
   (including the Copilot session's fleet PAT), and an app that leaked its env on an error page
   would otherwise get screenshotted and committed straight into git history." This is an
   env-unset applied **before spawning the E2E-booted app**, not a scrub of any captured output —
   it prevents the booted app's own process from ever being able to read those five names at all.
   Not relevant to an event-log's raw-tool-output capture point.

2. **`copilot_chat_model.secret_env_names()` (lines 677-703)**, used via `--secret-env-vars` at
   the actual CLI-invocation call site (line 340: `argv += ["--secret-env-vars",
   ",".join(sorted(secret_env_names()))]`) — this is the one mechanism that actually is "close to
   where a tool's raw output would need to be captured": it tells the **Copilot CLI itself** to
   scrub `COPILOT_SDK_AUTH_TOKEN`/`COPILOT_CONNECTION_TOKEN`/`COPILOT_GITHUB_TOKEN`/`GITHUB_TOKEN`
   out of its own shell/tool output before that output ever reaches this process's stdout parsing.
   Its own docstring is explicit that **Claude has no equivalent at all**: "the Claude CLI has no
   `--secret-env-vars` equivalent... so there is currently no redaction mechanism for anything a
   Claude turn's own shell output might echo — a real, previously-undocumented gap"
   (`copilot_chat_model.py:698-701`).

3. **`telemetry.py`'s `_B64_RUN` regex** (lines 32-36, `r"[A-Za-z0-9+/=_-]{40,}"`), applied in
   `traced_exec` (lines 147-168) to the `command` string before it becomes an OTEL span attribute
   — because `git_ops.push_head` embeds a credential-helper script inline in an exec'd command.
   This scrubs a span attribute, not any transcript a user would see.

Bottom line for the redesign: if the new event-log view needs to show raw tool output safely, the
only existing scrubbing that runs anywhere near that data today is Copilot's `--secret-env-vars`
(and only for Copilot); Claude turns currently have zero output redaction, so any raw-output
capture added for Claude needs its own new scrubbing built for it — nothing to reuse there.

## 7. Session/project data shape

`session_store.py`/`project_store.py` and their DB rows do carry `project_id`/`awaiting_gate`
(sessions) and `default_branch` (projects) — but **what the API actually returns is narrower**,
and one of the three is a real, confirmed gap.

`dbo.sessions` gets `project_id` (migration `0004_create_projects.sql`) and `awaiting_gate`
(same migration); `session_store._COLUMNS` includes both (`session_store.py:232-237`, quoted in
item 2). `dbo.projects` gets `default_branch` (migration `0005_add_project_default_branch.sql`);
`project_store._COLUMNS` includes it (`project_store.py:30-33`).

But `SessionResponse` — "the single schema-aware representation of a session row... the frontend
never queries SQL directly" (`sessions_api.py:264-267`) — is:

```python
# sessions_api.py:264-299
class SessionResponse(BaseModel):
    session_id: str
    owner: str
    repo: str
    user_login: str
    title: str
    source_branch: str
    work_branch: str
    run_id: str | None = None
    current_stage: str | None = None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    merge_ready: bool | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    failure_stage: str | None = None
    failure_type: str | None = None
    failure_message: str | None = None
    container_alive: bool = False
    awaiting_gate: bool | None = None
```

`awaiting_gate` **is** declared (last line above) — that part of the assumption holds. But
**`project_id` is not declared on `SessionResponse` at all**, despite being a real column read
into every row dict. The file's own self-check proves this is deliberate, not an oversight in my
reading: `_row_to_response` builds the model via `SessionResponse(**row, ...)`, and Pydantic v2's
default `extra="ignore"` silently drops any dict key the model doesn't declare
(`sessions_api.py:293-299` comment says this plainly), and the self-check pins it:

```python
# sessions_api.py:884-888
"project_id": "22222222-2222-2222-2222-222222222222", "awaiting_gate": True,
...
assert resp.awaiting_gate is True, resp
assert not hasattr(resp, "project_id"), "project_id should not be declared on SessionResponse"
```

`default_branch` was never a session-level field to begin with — it only ever lived on
`dbo.projects`/`ProjectResponse` (`sessions_api.py:677-695`):

```python
class ProjectResponse(BaseModel):
    project_id: str
    name: str
    owner: str | None
    repo: str | None
    tech_stack_id: str | None
    tech_stack_text: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    default_branch: str | None
```

**Net for a run-detail page:** `GET /sessions/{id}` today hands back everything in
`SessionResponse` above (including `awaiting_gate` and `container_alive`) but *not* `project_id` —
so a run-detail page cannot currently learn which project a session belongs to from that endpoint
alone; it would need to already know the `project_id` from whatever navigation got it there, or the
API needs a small change to stop dropping it. `default_branch` is reachable only via a separate
`GET /projects` (or a future `GET /projects/{id}`) call, keyed by that same `project_id` — it was
never meant to come from the session endpoint at all.

## 8. Stage names / StageSpec

The exact 8 stage keys, in order, as they exist right now in the `STAGES` list
(`agent/src/graph.py:1102-1379`):

| # | key | StageSpec line | `requires_human_gate` |
|---|-----|-----|-----|
| 1 | `tech-stack` | 1104 | `True` (explicit, line 1112) |
| 2 | `specification` | 1126 | `True` (default — not overridden) |
| 3 | `plan` | 1148 | `True` (default — not overridden) |
| 4 | `ac-to-tests` | 1181 | `False` (line 1189) |
| 5 | `minimal-code-to-green` | 1248 | `False` (line 1262) |
| 6 | `remediation` | 1294 | `False` (line 1302) |
| 7 | `adversarial-compliance` | 1323 | `False` (line 1331) |
| 8 | `metrics-exit` | 1350 | `False` (line 1358) |

These map onto the brief's 8 named stages exactly (Tech Stack, Speccing, Plan, Generating Failing
Tests, Building, Remediation, Adversarial Review, Metrics Exit). The list literally closes at
`graph.py:1379`.

Two more keys exist in `GraphState.stages` but are **not** in this list, worth knowing so a stage
picker/tab list doesn't miss or double-count them (`graph.py:2882-2893`):
- `raw-requirements` — no `StageSpec` at all; a deterministic record-only pseudo-stage (comment,
  `graph.py:1120-1124` and `:2891-2892`).
- `BROWNFIELD_BASELINE_SPEC` (`graph.py:2666`, collected into `_STANDALONE_STAGE_SPECS`,
  `graph.py:2887-2889`) — a real `StageSpec` but deliberately outside the flat `STAGES` list
  because "a bespoke cluster sits between it and its neighbors" (comment, `graph.py:2882-2886`).

Only 3 of the 8 (`tech-stack`, `specification`, `plan`) ever actually call `interrupt()` and show a
pending human-gate state (item 4) — the other 5 are deterministic-verify-or-auto-approve only, so a
"Gate approve/reject UI" literally has nothing to render for 5 of the 8 stage tabs; those 5 only
ever show pass/fail/retrying.

---

## Gaps vs. the original plan's assumptions

1. **No unified event vocabulary exists to consume.** One (`SessionEventType`) existed before Part
   1's CLI-exec rewrite and was deliberately deleted; nothing replaced it. Any "folding tool-call
   rows" / wall-clock swimlane view needs a **brand-new backend schema and capture point** — there
   is no existing translation layer to plug a new UI into. Today, both providers parse their raw
   CLI output (Claude: one terminal JSON object; Copilot: a JSONL stream, and only the *last* line
   of that stream is even read) and immediately collapse it to one final message + a token/cost
   dict, discarding everything else in the same function call.

2. **Nothing survives a container teardown.** The only per-turn, timestamped record that exists at
   all (`.ai-dev-workflow/ledger.jsonl`) lives solely inside the disposable sandbox's own working
   tree, is reset on every fresh run, and is never git-committed or written to the DB. If the plan
   assumed some kind of backend history to backfill an event log from after the fact, it does not
   exist past the life of the container.

3. **`project_id` is silently dropped from the session API today**, despite being a real DB column
   session_store.py reads back on every row — confirmed by the file's own self-check assertion
   (`sessions_api.py:888`). If the plan assumes a run-detail page can read its own `project_id` off
   `GET /sessions/{id}`, that assumption is false as written; either the frontend must already know
   it from navigation context, or `SessionResponse` needs a one-line addition.

4. **`default_branch` was never a session-level field** — it's project-level only. If anything in
   the plan says "the session carries its default branch," that needs to become "the session's
   `project_id` is used to separately fetch the project, which carries `default_branch`."

5. **There is no gate approve/reject REST endpoint, and there never was one to begin with.** The
   only named action `sessions_api.py` exposes is `"refresh-secrets"`. Resolving a paused gate is
   entirely LangGraph's `interrupt()`/resume mechanism, reached only through the AG-UI bridge
   mounted at FastAPI's bare `/` — which itself is only reachable through the Next.js
   `/api/copilotkit` CopilotRuntime hop, not directly. A custom Gate UI has exactly two real
   options: keep depending on `@copilotkit/react-core`'s interrupt hooks (`useInterrupt` /
   `useHumanInTheLoop` / `useAgent`) to actually deliver the resume payload even while dropping
   `@copilotkit/react-ui`'s chat components (this matches the stated architectural intent of
   "keep the protocol/state layer, drop only the chat UI"), or add wholly new backend plumbing —
   this is not something already solved on the backend that the redesign can just point a new
   component at.

6. **Cost is tracked but nowhere live.** `_last_usage["cost"]` is computed every turn but is
   dropped before it ever reaches an OTEL span (only tokens + model name are attached); it is only
   ever summed, once, when `metrics-exit` runs, by reading back the ephemeral ledger file. A
   "cost/token display" in the new UI has no live number to bind to today for either provider —
   this needs new plumbing, not a new view onto an existing number.

7. **The "secret-stripping" pointer in `e2e_nodes.py` is a false lead for the event-log design.**
   That mechanism protects the E2E-booted end-user application's own process environment, not the
   coding agent's tool-call output. The actual analog — scrubbing a coding agent's own raw output
   before it could be captured/displayed — exists only for Copilot (`--secret-env-vars`, 4 named
   env vars) and **does not exist at all for Claude**, a real, currently-undocumented-until-now gap
   the module's own author already flagged. Any new "raw tool output" pane in the redesign needs
   its own redaction story for Claude turns; there is nothing to inherit.
