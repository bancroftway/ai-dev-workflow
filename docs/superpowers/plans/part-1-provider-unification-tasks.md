# Part 1 — Unified provider layer: task breakdown

**Spec (binding authority):** `C:\Users\jblis\.claude\plans\inside-the-staging-container-sunny-tome.md`
— this task file implements Part 1 only (Rollout and sequencing: "Part 1 first, on its own").
Parts 4/3/2 are out of scope for this task file; do not touch Settings UI, ticket/board data
model, or frontend transcript UI.

Read the Spec's Part 1 in full before starting Task 1 — this file gives concrete, bite-sized
specs per task, but the Spec has the full rationale, the verified CLI flag tables, and the
"What this simplifies" reasoning behind every design choice below. Where this file and the Spec
disagree, the Spec wins; flag the conflict in your report rather than silently picking one.

## Global Constraints (apply to every task)

- Repo root: `d:\Projects\bancroftway\ai-dev-workflow`. Almost all work is under `agent/`.
- Python 3.12, existing venv at `agent/.venv` (Windows). Run tools as
  `agent/.venv/Scripts/python.exe`, not a bare `python`.
- Match the existing codebase's style exactly: dense "why, not what" comments only where a
  decision needs justifying (cite the real reason — an observed bug, a measured tradeoff — the
  way `copilot_chat_model.py`, `ac_coverage_gate.py`, `write_scope_gate.py` already do). No
  comments explaining what a line does. No speculative abstractions, no config for values that
  never vary, no unrequested error handling for cases that can't happen.
- **Every new or rewritten module needs a `_demo()` self-check function** (pure-logic assertions,
  `if __name__ == "__main__":` dispatch through the package name — copy the exact re-dispatch
  pattern `copilot_chat_model.py`'s own `__main__` block uses and explains, since importing this
  file directly instead of through the package creates a second copy of its module-level dicts).
  This is a hard repo convention, not optional polish.
- After every task: from `agent/`, run
  `.venv/Scripts/python.exe -m py_compile <every touched .py file>`, then verify the whole app
  still imports clean under **both** provider values:
  ```
  .venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'.'); from src import graph; import main"
  AGENT_PROVIDER=claude .venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'.'); from src import graph; import main"
  ```
  A task is not done if either import fails.
- Never commit secrets. Never touch `.env`.
- This is a live production pipeline, not a greenfield project — anything this task doesn't
  explicitly change must keep working exactly as it does today.
- No subagents: implementers never dispatch their own subagents or reviewers.

---

## Task 1: Shared CLI-exec runner module

**Files:** new `agent/src/cli_agent_exec.py`.

Both providers become per-turn subprocess exec inside the sandbox (Spec Part 1, "The pivot").
Factor out what's now identical between them into one shared module, since only the argv-building
and per-line/whole-output JSON parsing actually differ per provider.

Build these pieces, generic over provider (no Claude- or Copilot-specific logic in this file):

1. **Scratch-file writer**: `async def write_scratch_file(provider, thread_id, path, content) -> None`.
   Base64-encode `content`, write via chunked `printf %s <chunk> >> file` execs when the encoded
   payload exceeds a budget constant (`_EXEC_CMD_BUDGET = 16000`, matching
   `agent/src/repo_files.py`'s own `_EXEC_CMD_BUDGET` for the identical reason — Windows
   `CreateProcess`'s ~32K argv limit). Base64 alphabet (`A-Za-z0-9+/=`) contains no shell
   metacharacters, so chunks are interpolated unquoted into the command exactly like
   `repo_files.write_repo_file` already does — read that function first and mirror its shape,
   targeting an arbitrary absolute path (this one is NOT repo-relative, so do not call
   `validate_repo_relative_path` — that's the one real difference from `repo_files`' version).

2. **Backgrounded turn runner**: `async def run_turn(provider, thread_id, command: str, prompt: str, scratch_prefix: str, timeout_seconds: float) -> TurnResult` where `command` is the
   provider-specific CLI invocation (argv already built by the caller, as a single shell-safe
   string via `shlex.join`), and `TurnResult` is a small dataclass:
   `{stdout: str, stderr: str, exit_code: int}`. Internally:
   - Write `prompt` to `{scratch_prefix}` via `write_scratch_file`.
   - Launch backgrounded: `setsid nohup sh -c '<command> < {prompt_path} > {prompt_path}.out 2> {prompt_path}.err; echo $? > {prompt_path}.exit' >/dev/null 2>&1 & echo $! > {prompt_path}.pid`
     — **use `;` before the backgrounded `setsid`, never `&&`**: with `&&`, `cmd1 && cmd2 &`
     backgrounds the whole compound as one job, so `$!` would report the wrong PID and a
     timeout-kill would target the wrong process group. This exact bug and its fix are documented
     inline in the reference implementation the Spec describes — get this right the first time,
     it's easy to get subtly wrong and hard to notice in testing.
   - Poll every 5 seconds (`test -f {prompt_path}.exit && echo DONE || echo PENDING`) via
     `provider.exec_in_sandbox` — each poll is itself activity that resets the sandbox idle
     reaper's clock, which is the actual reason this is backgrounded-and-polled instead of one
     blocking exec (a multi-minute turn would otherwise only touch the idle clock once, at the
     start).
   - On timeout (`timeout_seconds` exceeded with no `.exit` file): best-effort
     `kill -TERM -$(cat {prompt_path}.pid) 2>/dev/null; kill -KILL -$(cat {prompt_path}.pid) 2>/dev/null; true`,
     then raise `TimeoutError`.
   - On completion: `cat` `.out`/`.err`/`.exit`, then `rm -f {prompt_path}*` to clean up (this glob
     also removes any sibling scratch file sharing the prefix, e.g. an MCP config file — fine,
     that's the intended cleanup scope).
   - Non-zero exit code: raise `RuntimeError` with the exit code and a truncated (first 2000 chars)
     stderr tail — do not raise on non-zero here if the caller needs to distinguish cases (see
     Task 2/3 note); expose exit code, stdout, stderr on `TurnResult` and let each provider module
     decide what's an error, since Claude and Copilot signal errors differently in their own JSON.

3. **Constants**: `_SCRATCH_DIR = "/tmp/aidw-agent"` (renamed from the Claude-only
   `/tmp/aidw-claude` the Spec's earlier draft used — this module is shared, so the path must not
   imply one provider), `_POLL_INTERVAL_SECONDS = 5.0`.

4. Add `CLI_AGENT_TURN_TIMEOUT_SECONDS` to `agent/src/config.py` (default `2400`, replaces the
   Claude-only-named constant an earlier draft of this plan used) — same style as the existing
   `E2E_MAX_FIX_CYCLES`-style constants in that file (env-overridable, one-line comment on why the
   default is generous, matching that file's existing comment density).

**Self-check**: pure-logic assertions on the command-string construction (the `;` vs `&&`
semantics are worth a literal string-shape assertion, not just prose) and the chunking boundary
math. The actual exec/backgrounding path needs a live sandbox and cannot be unit-tested here —
say so in the demo's docstring, matching how `copilot_chat_model.py`'s own demo scopes itself to
"the pure half."

---

## Task 2: Claude provider module

**Files:** new `agent/src/claude_chat_model.py`. Depends on Task 1.

Implement `ClaudeChatModel(BaseChatModel)` and the module-level functions
`get_chat_model_for_thread`, `close_thread_session`, `forget_thread_sessions`, `close_session`,
`get_session_id`, `read_skill_invocations`, `secret_env_names`, using `cli_agent_exec.run_turn`
for the actual turn.

**Exact verified `claude` CLI flags** (do not guess others — these are the only ones confirmed
live against the installed CLI this session):

| Purpose | Flag |
|---|---|
| One-shot prompt (via stdin, not argv) | `-p` |
| Resume | `--resume <session_id>` (omit entirely on first turn) |
| Output | `--output-format json` — single JSON object with `session_id`, `result`, `usage` (`input_tokens`/`output_tokens`), `total_cost_usd`, `is_error`, `stop_reason` |
| Permission/write mode | `--permission-mode <mode>` |
| Tool allow/deny | `--tools <comma list>` / `--disallowedTools <comma list>` |
| Plugin dirs | `--plugin-dir <path>` (repeatable) |
| Structured output | `--json-schema '<json schema string>'` |
| Model | `--model <name>` |
| Custom agents | `--agents <json>` / `--agent <name>` |
| MCP | `--mcp-config <path or @file>` |
| Session display name | `--name <name>` |

Map the shared kwarg vocabulary (same field names `copilot_chat_model.py`'s `CopilotChatModel`
already uses — `agent_mode`, `available_tools`, `excluded_tools`, `pre_tool_use_hook`,
`mcp_servers`, `custom_agents`, `agent`, `tools`, `disabled_skills`, plus a **new Claude-only**
`response_schema: type[BaseModel] | None` field) onto these flags:

- `agent_mode`: `{"plan": "plan", "autopilot": "bypassPermissions", "shell": "bypassPermissions", "interactive": "default"}.get(agent_mode, "default")` → `--permission-mode`.
- `available_tools`/`excluded_tools`: Copilot-vocabulary strings (`"builtin:view"`, `"builtin:edit"`,
  etc.) map to Claude tool names via `{"builtin:view": "Read", "builtin:grep": "Grep", "builtin:glob": "Glob", "builtin:edit": "Edit", "builtin:create": "Write", "builtin:apply_patch": "Edit", "builtin:bash": "Bash", "builtin:skill": "Skill"}`. Unmapped names (`builtin:task_complete`,
  `builtin:ask_user`) are dropped with a `logger.warning`, never silently ignored. `available_tools`
  set → `--tools`; else `excluded_tools` set → `--disallowedTools`.
- `pre_tool_use_hook` set: `logger.warning` that Layer 1 write-scope enforcement has no CLI
  equivalent (Layer 2's `git diff` gate in `write_scope_gate.py` is authoritative regardless) —
  do not attempt to translate it into a flag.
- `tools` (the Copilot SDK terminal-tool objects) set: `logger.warning` that this mechanism doesn't
  exist for Claude — `response_schema` is the replacement, used by `stack_runner.py` (Task 9).
- `disabled_skills` (falls back to `config.COPILOT_DISABLED_SKILLS` when unset, same as the
  Copilot model does today): non-empty → `--append-system-prompt "Do not invoke these skills this
  turn under any circumstances: {', '.join(names)}."` — soft instruction, no hard CLI enforcement
  exists.
- `self.sandbox is not None` → `--plugin-dir <path>` once per entry in
  `config.COPILOT_PLUGIN_DIRECTORIES` (reused as-is — same plugin-marketplace format both CLIs
  consume).
- `response_schema` set → `--json-schema {json.dumps(response_schema.model_json_schema())}`.

**Session identity**: `_session_ids: dict[str, str]` keyed `"{thread_id}:{stage}:{role}"` (same
key shape as Copilot's `_sessions` dict) → the CLI's own `session_id` from the parsed JSON result.
No client/connection object to hold — eviction is a pure dict pop, `close_thread_session`/
`forget_thread_sessions` never make a network call (state this in a docstring the way
`copilot_chat_model.py`'s own eviction functions explain *why* they're sync/network-free).

**Turn execution** (`_agenerate_inner`): flatten `messages` to one prompt string exactly like
`copilot_chat_model._messages_to_prompt` does for the text case (`SystemMessage` → `"Instructions:\n{text}"` prefix, others verbatim, joined `"\n\n"`) — **multimodal (list-shaped) content**:
extract only `{"type": "text"}` parts, join with `"\n"`, and `logger.warning` how many non-text
parts were dropped (do not attempt image support — out of scope, note as a ponytail-style comment
with the upgrade path: mirror `write_scratch_file` for image bytes and pass `--file` if ever
needed). Call `cli_agent_exec.run_turn`, parse the JSON result, raise `RuntimeError` if
`is_error` is true or the JSON fails to parse, store `session_id`, populate a `_last_usage`
`PrivateAttr` from `usage`/`total_cost_usd` (same shape as `copilot_chat_model.py`'s own
`_last_usage`, for the OTEL span attributes `_agenerate` already sets — copy that wrapping
unchanged from `copilot_chat_model.py`, it's provider-agnostic).

**`read_skill_invocations(provider, thread_id, session_id)`**: read
`~/.claude/projects/-workspace-repo/{session_id}.jsonl` (the slug is a **fixed string** —
`-workspace-repo` — because every Claude turn in this pipeline runs with cwd `/workspace/repo`,
and Claude Code's project-transcript directory name is every non-alphanumeric character in the
cwd replaced with `-`; this was confirmed against a real local transcript this session, but not
against a real Linux container — if the real path differs, fail open (return `None`), matching
`skill_gate.py`'s existing "unverifiable" contract, never report a false empty list). Parse each
line as JSON; for entries where `type == "assistant"`, walk `message.content` for blocks where
`type == "tool_use" and name == "Skill"`, collect `input.skill`. This exact shape
(`{"type":"tool_use","name":"Skill","input":{"skill":"<name>"}}` inside an `assistant` entry) was
confirmed against a real transcript this session — implement it as specified, don't re-derive.

**`secret_env_names()`** → `{"ANTHROPIC_API_KEY"}`.

**Self-check**: tool-name mapping table (assert known + unknown-dropped cases), session-cache
eviction (doomed/survivor threads, mirroring `copilot_chat_model._demo`'s own eviction test
shape — read that function and copy its structure, not its content).

---

## Task 3: Copilot provider module (rewrite)

**Files:** rewrite `agent/src/copilot_chat_model.py` in place — same public API names as Task 2
so `chat_model.py` (Task 4) can dispatch identically, but the SDK-based implementation is fully
replaced by a CLI-exec implementation using `cli_agent_exec.run_turn`, matching Task 2's shape.
Depends on Tasks 1 and 2 (mirror Task 2's structure closely — argv-builder + JSON/JSONL parser
over the same shared runner, not a second bespoke turn-execution path).

**Exact verified `copilot` CLI flags** (from GitHub's current reference docs, not recalled):

| Purpose | Flag |
|---|---|
| One-shot prompt | `-p PROMPT` / `--prompt=PROMPT` (or piped stdin) |
| Resume | `-r, --resume[=session_id]` or `--session-id <id>` (prefer `--session-id` for exact-match resume; `--resume` alone opens an interactive picker requiring a TTY, which a headless exec never has) |
| Output | `--output-format=json` → **JSONL, one JSON object per line** (not one terminal object like Claude) |
| Mode | `--mode interactive\|plan\|autopilot` (already matches this codebase's existing `agent_mode` Literal verbatim — no translation table needed here, unlike Claude) |
| Tool allow/deny | `--available-tools=<list>` / `--excluded-tools=<list>` / `--allow-tool=` / `--deny-tool=` |
| Plugin dirs | `--plugin-dir=<path>` (repeatable) |
| No interactive pause | `--no-ask-user` |
| Secret redaction | `--secret-env-vars=<list>` |
| Model | `--model=<name>` |
| MCP | `--additional-mcp-config=<json or @file>` |
| Custom agent | `--agent=<name>` |
| Internal task-wait timeout | `COPILOT_TASK_WAIT_TIMEOUT_SECONDS` env var (default 600s) — **set this explicitly** on the sandbox exec environment to match `CLI_AGENT_TURN_TIMEOUT_SECONDS`, otherwise Copilot's own `-p` process can return early on a legitimately long backgrounded shell command it spawned, before our own outer timeout ever fires |

Map the same shared kwarg vocabulary Task 2 defines:
- `agent_mode` maps directly (`"interactive"|"plan"|"autopilot"|"shell"` — pass through as-is to
  `--mode`; if the CLI rejects `"shell"` as an unknown mode value, fall back to `"autopilot"` and
  log it — verify against the real CLI, don't assume).
- `available_tools`/`excluded_tools`: Copilot's own vocabulary is **already** `--available-tools`'s
  native shape (this codebase's `"builtin:view"` etc. strings) — pass through, no mapping table
  needed (contrast with Claude, which needs translation).
- `pre_tool_use_hook`, `tools`, `disabled_skills`: same "no CLI equivalent, log and continue"
  treatment as Task 2, for the same reasons — Copilot's own SDK-level hook/terminal-tool
  capabilities were real only over the now-retired persistent session.
- `--no-ask-user` always passed (matches BR-6 full-authority mode — no interactive pause is ever
  wanted here).
- `--secret-env-vars` always includes whatever `secret_env_names()` returns for this module.

**Session identity**: same `_session_ids: dict[str,str]` shape as Task 2. Extract `session_id`
from the JSONL stream — **this exact per-line event shape has not been sampled against a real
`copilot -p --output-format=json` invocation as of writing this task.** Parse defensively: each
line is one JSON object; look for a `session_id` field on any line that has one (the final/result-
shaped line is the most likely place, by analogy with Claude's single-object shape, but confirm
against real output before hardening this). **This is the highest-risk unverified piece in this
whole task file — budget real time to run a real `copilot -p --output-format=json` invocation
inside an actual container (or locally if `copilot` CLI is installed and authenticated) and adjust
the parser to match reality, rather than shipping a guess.** Report exactly what you found in your
task report, including a raw sample of the JSONL output.

**`read_skill_invocations`**: the old SDK-server path read
`~/.copilot/session-state/<session_id>/events.jsonl`, written by the now-retired server process.
**Do not assume a bare `-p` invocation writes an equivalent file** — verify this against a real
run first. If no such log exists in headless CLI mode, return `None` unconditionally (fail open,
matching `skill_gate.py`'s existing contract) and say so plainly in your report — this may mean
skill-invocation verification is unavailable for Copilot until GitHub's CLI exposes something
better, which is a real, reportable limitation, not a bug to paper over.

**`secret_env_names()`** → `{"COPILOT_SDK_AUTH_TOKEN", "COPILOT_CONNECTION_TOKEN", "GITHUB_TOKEN"}`
(unchanged from today's behavior).

**Self-check**: same shape as Task 2's, plus whatever the JSONL parser turns out to need once
verified against real output.

---

## Task 4: Provider dispatch + structured output

**Files:** new `agent/src/chat_model.py`, new `agent/src/structured_output.py`. Depends on
Tasks 2 and 3.

`structured_output.py`: move `ainvoke_structured` here verbatim from wherever it lives today
(it's currently defined inside `copilot_chat_model.py`, but its logic only ever calls
`model.ainvoke()` on a plain `BaseChatModel` — nothing provider-specific about it). Same function
signature, same JSON-schema-prompt-and-validate-and-retry behavior, same
`_STRUCTURED_OUTPUT_INSTRUCTION`/`_CODE_FENCE_RE` constants. Self-check: the regex/formatting
assertions only (the retry loop itself needs a live model).

`chat_model.py`: `PROVIDER = os.environ.get("AGENT_PROVIDER", "copilot")` at module scope
(mirrors `agent/src/sandbox/factory.py`'s exact `SANDBOX_PROVIDER` pattern — read that file first
and copy its shape). `if PROVIDER == "copilot": from .copilot_chat_model import (...)` /
`elif PROVIDER == "claude": from .claude_chat_model import (...)` / `else: raise ValueError`,
re-exporting `get_chat_model_for_thread`, `close_thread_session`, `forget_thread_sessions`,
`close_session`, `get_session_id`, `read_skill_invocations`, `secret_env_names` from whichever
module is active, plus `ainvoke_structured` imported directly from `structured_output` (not
dispatched — it's provider-agnostic). Self-check: both branches import cleanly (parametrize or
just assert `PROVIDER in ("copilot","claude")` and that every re-exported name is callable).

---

## Task 5: Sandbox readiness unification

**Files:** `agent/src/sandbox/provider.py`. Depends on nothing above (can run in parallel with
Tasks 1-4, but review it after Task 6 since Task 6 is the actual caller).

Add `async def wait_for_cli_ready(exec_fn: Callable[[str], Awaitable[tuple[int,str,str]]]) -> None`
alongside the existing `wait_for_copilot_ready` (do not remove `wait_for_copilot_ready` — it's
still used on the Copilot path per Task 6). No server/socket to handshake with anymore for either
provider's CLI-exec path — poll `exec_fn("claude --version")` (or `"copilot --version"`, caller's
choice — this function takes whichever version-check command the caller wants) every 0.5s up to
the existing `_READY_TIMEOUT_SECONDS` (60.0), raise `RuntimeError` on timeout with the last error.
`exec_fn` is a thin provider-specific wrapper (docker exec / az container exec) the caller
supplies — this function runs **before** the caller has registered the sandbox in its own
bookkeeping, same ordering `wait_for_copilot_ready` already requires of every existing caller, so
it cannot go through the higher-level `SandboxProvider.exec_in_sandbox` (which looks the session
up in that bookkeeping).

Self-check: none needed beyond what already exists for `wait_for_copilot_ready` — this is a
small, mechanical addition to a file whose existing pure-logic paths are already covered.

---

## Task 6: Sandbox provisioning updates

**Files:** `agent/src/sandbox/local_docker.py`, `agent/src/sandbox/azure_aci.py`. Depends on
Task 5 (and conceptually Task 4, for the `PROVIDER` constant, imported **lazily inside the
function body**, never at module scope — see the existing lazy-import comments in this exact file
for `forget_thread_sessions` and copy that reasoning: `chat_model` imports whichever provider
module is active, which imports `.sandbox`, so a module-scope import here would cycle).

In both files' `provision()`:
- Rename the `copilot_auth_token: str` parameter to `runtime_auth_token: str` (also rename it in
  `sandbox/provider.py`'s abstract method signature and docstring, and in every caller —
  `sessions_api.py`, `run_headless.py`, see Task 11).
- `from ..chat_model import PROVIDER` (lazy, function-local).
- Both `COPILOT_SDK_AUTH_TOKEN` and `ANTHROPIC_API_KEY` env vars get set on every container/
  container-group **unconditionally**, one real one empty:
  `f"COPILOT_SDK_AUTH_TOKEN={runtime_auth_token if PROVIDER == 'copilot' else ''}"` /
  `f"ANTHROPIC_API_KEY={runtime_auth_token if PROVIDER == 'claude' else ''}"`. Also add
  `AGENT_PROVIDER={PROVIDER}` as its own env var (entrypoint.sh needs it — Task 7). This keeps
  `local_docker.py`'s `docker create` call and `azure_aci.py`'s `az container create` call from
  ever needing to branch on provider themselves — harmless unused env either way.
- **Remove** the `-p {host_port}:3000` port publish in `local_docker.py` and the
  `COPILOT_SERVER_PORT`/`COPILOT_CONNECTION_TOKEN` env vars in both files — nothing listens on a
  port anymore for either provider. (Keep `_COPILOT_PORT_IN_CONTAINER`/`_free_port()`-adjacent
  code only if something else in the file still needs a port number for its `SandboxSession`
  return value shape — check whether `SandboxSession.port`/`.connection_token` fields are read
  anywhere else before deciding whether to keep them as always-empty/zero placeholders or thread
  the dataclass shape change further. Default to keeping the dataclass shape unchanged and just
  passing dummy/empty values, since changing `SandboxSession`'s fields ripples into
  `copilot_chat_model.py`/`claude_chat_model.py`'s `sandbox: SandboxSession | None` field — out of
  scope for this task, note it as a follow-up if it looks genuinely dead.)
- Replace the direct `wait_for_copilot_ready(...)` call with a branch:
  `if PROVIDER == "copilot": await wait_for_copilot_ready(...)` (unchanged) `else:` build a local
  `_exec` closure wrapping this provider's own low-level command runner (`_run_docker("exec", "-w",
  WORKSPACE_DIR_IN_CONTAINER, container_id, "sh", "-c", cmd)` for local; the `az container exec`
  equivalent for ACI) and `await wait_for_cli_ready(_exec)`. Apply this to **every** call site of
  `wait_for_copilot_ready` in `local_docker.py`, including the one inside `_try_reattach` — a
  reattach to a Claude-mode container needs the same branch, not just fresh provisioning.

Self-check: none new — this is provisioning logic against a real Docker daemon, covered by
Task 12's real-container verification, not a unit test.

---

## Task 7: Dockerfile + entrypoint.sh

**Files:** `agent/sandbox-image/Dockerfile`, `agent/sandbox-image/entrypoint.sh`. Depends on
nothing above (can run in parallel), but review it alongside Task 6 since they share the
`AGENT_PROVIDER` env var contract.

**Dockerfile**: add `ARG CLAUDE_CODE_CLI_VERSION=2.1.126` near the existing
`ARG COPILOT_CLI_VERSION=1.0.79` (same pinning philosophy — comment explaining why a version is
pinned, matching that file's existing comment style). Install via npm
(`npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_CLI_VERSION}"`) in the same layer as the
existing Node/npm/gh install block (Node 22 is already present there) — do **not** use the
curl+tar fetch-stage pattern that's used for the Copilot binary; that pattern exists for
GitHub-releases-hosted prebuilt binaries, npm is simpler and matches how `eslint`/`typescript`/
`playwright` are already installed in this exact file. Both CLIs get baked into the one image
regardless of which `AGENT_PROVIDER` a given deployment runs — decided at container start, not
build time. Remove `EXPOSE 3000` — nothing listens on a port anymore for either provider.

**entrypoint.sh**: after the existing clone/bootstrap logic and before the current
`copilot --server` exec, add: `AGENT_PROVIDER="${AGENT_PROVIDER:-copilot}"`; if `"claude"`, warn
if `ANTHROPIC_API_KEY` is empty (same style as the existing `COPILOT_SDK_AUTH_TOKEN` empty-warning
a few lines below) and `exec sleep infinity` (still makes this process pid 1's replacement, same
signal-delivery reasoning as the `copilot` exec path — this process still IS the container's main
process, it just has nothing to serve). Otherwise, fall through to the existing unchanged
`exec copilot --headless ...` path. Also update the copy in this file's own header comment
describing entrypoint responsibility #2 to mention both providers, not just Copilot.

Self-check: N/A — shell scripts and a Dockerfile, verified by an actual `docker build` +
container run in Task 12, not a unit test.

---

## Task 8: Skill gate provider dispatch

**Files:** `agent/src/gates/skill_gate.py`. Depends on Task 4.

Change `from ..copilot_chat_model import get_session_id` to
`from ..chat_model import get_session_id, read_skill_invocations`. Rewrite `invoked_skills()`:

```python
async def invoked_skills(provider, thread_id, stage, role="draft"):
    session_id = get_session_id(thread_id, stage, role)
    if not session_id:
        return None
    return await read_skill_invocations(provider, thread_id, session_id)
```

Delete the inline path-construction and JSONL-parsing logic this function currently has (it moves
into each provider module per Tasks 2/3 — `copilot_chat_model.read_skill_invocations` and
`claude_chat_model.read_skill_invocations`). Remove the now-unused `_SESSION_STATE_DIR` module
constant and the `json`/`shlex` imports if nothing else in the file needs them (check before
removing).

Everything else in this file (`check_required_skills`, `skills_record`, `feedback_for`, the
`_demo`) is provider-agnostic already — leave unchanged.

Self-check: this file's existing `_demo()` should still pass unmodified — confirm, don't rewrite
it unless the import changes above break something specific.

---

## Task 9: stack_runner.py unification

**Files:** `agent/src/stack_runner.py`. Depends on Task 4 (for `chat_model.PROVIDER`) and Task 2
(for `response_schema`).

The Copilot-only terminal-tool mechanism (`_make_report_tool`, the `Tool`/`ToolResult` imports
from the `copilot` package, `REPORT_TOOL_NAME`, the `_STASHES` dict) is SDK-only and no longer
has a home now that Copilot is also CLI-exec. Replace the whole reporting mechanism with one
path, used by both providers: `structured_output.ainvoke_structured` (Task 4) against the stage's
`schema`, with a same-shape "no report → nudge once → give up" retry loop as today's — the
JSON-schema-prompt-and-validate approach already handles both providers uniformly since it only
calls `.ainvoke()`.

Concretely: delete `_make_report_tool`, `REPORT_TOOL_NAME`, `_STASHES`, the `from copilot import
Tool` / `from copilot.tools import ToolResult` imports (these become dead once nothing
instantiates a `Tool` object — leaving them would break module import for any deployment that no
longer has `github-copilot-sdk` installed, see Task 10). Rewrite `run_and_report` to build the
model via `get_chat_model_for_thread(..., agent_mode="autopilot", available_tools=tools_allowlist)`
(no `tools=` kwarg anymore) and call
`report = await ainvoke_structured(model, messages, schema)` wrapped in the same
try/except-synthesize-failure-report shape the function already has, replacing the
`_STASHES.pop(stash_key, None)` check with whatever `ainvoke_structured` returns (it either
returns a valid `schema` instance or raises after exhausting its own retries — catch that and
synthesize the same `success=False` report shape this function already produces on other failure
paths). Keep `WELL_FORMED_JSON_RULES`/`SKILLS_REPORT_RULES` — those prompt strings are
provider-agnostic reporting instructions, not tied to the tool mechanism; adjust their wording
only if they reference "the `report_stage_output` tool" in a way that's now inaccurate (they do —
reword to describe "respond with the JSON object" instead of "call the tool").

Self-check: N/A for this file historically (it has none today) — do not add one; this function's
logic is thin orchestration over `ainvoke_structured`, which already has its own.

---

## Task 10: Dependency + model config

**Files:** `agent/pyproject.toml`, `agent/config/models.yaml`, `agent/src/model_config.py`.
Depends on Tasks 3 and 9 (both must have removed their `copilot`-package imports first, or this
task's dependency removal breaks module import).

**pyproject.toml**: remove the `github-copilot-sdk` dependency line. Before removing, grep the
whole `agent/` tree for `from copilot import` / `import copilot` / `from copilot.` to confirm
nothing outside `stack_runner.py` (Task 9) and the old `copilot_chat_model.py` (Task 3 already
rewrote it) still imports the package — report anything else found rather than silently leaving
it broken.

**models.yaml**: restructure every stage entry to nest under a provider key —
`{stage}: {copilot: {draft_model: ..., audit_model: ...}, claude: {draft_model: ..., audit_model: ...}}`
— draft/audit both move under each provider, neither collapses. For the `copilot` section, copy
today's existing values verbatim (zero behavior change for existing Copilot deployments). For the
`claude` section, use real Claude model aliases (`"sonnet"`, `"opus"`, `"haiku"`) as a reasonable
starting default — note in a YAML comment that these are placeholders an operator should tune,
exactly like the file's existing header comment already tells operators to check
`CopilotClient.list_models()` for the real roster (mirror that same "these values need real
verification, here's how" framing for Claude).

**model_config.py**: `get_model_name(stage, role)` gains a required `provider` parameter (or reads
it from `chat_model.get_provider()`-equivalent if Task 4/Part 4 wiring already exists — for this
task, since Part 4 hasn't shipped yet, take `provider: str` as an explicit parameter and have
every call site pass `chat_model.PROVIDER`). Every internal lookup (`_load_config().get(stage, {})`
today) becomes `_load_config().get(stage, {}).get(provider, {})`. Update every call site of
`get_model_name` across the codebase (grep for it — `graph.py`, `stack_runner.py`,
`e2e_nodes.py`, `test_hardening_nodes.py`, etc.) to pass the provider argument.

Self-check: extend `model_config.py`'s existing test coverage (or add a `_demo()` if none exists —
check first) asserting both providers resolve distinct, correct values for at least one stage with
an explicit `audit_model` and one that falls back to `draft_model`.

---

## Task 11: Call-site wiring

**Files:** `agent/src/sessions_api.py`, `agent/run_headless.py`, `infra/main.bicep`. Depends on
Task 6 (the `runtime_auth_token` rename) and Task 4.

**sessions_api.py**: in `provision_session`, compute
`runtime_auth_token = os.environ.get("ANTHROPIC_API_KEY", "") if chat_model.PROVIDER == "claude" else os.environ.get("GITHUB_TOKEN", "")`
(import `chat_model` — check for an existing import cycle risk here the way Task 6 had to guard
against; `sessions_api.py` is not imported by `chat_model.py`'s dependency chain, so a top-level
import should be safe, but verify by running the Task-12 import check after this change) and pass
it as `runtime_auth_token=` to `provider.provision(...)` instead of `copilot_auth_token=`.

**run_headless.py**: same computation, replacing the `copilot_token` local variable; update the
error message when the token is missing to name the right env var
(`ANTHROPIC_API_KEY` vs `GITHUB_TOKEN`) depending on `chat_model.PROVIDER`; rename the
`copilot_auth_token=` kwarg at the `provider.provision(...)` call site to `runtime_auth_token=`.
Also import `chat_model` instead of `copilot_chat_model` for the `close_thread_session` call at
teardown (mechanical — the function name is unchanged, only the module it's imported from).

**infra/main.bicep**: add two new params mirroring the existing `copilotGithubToken` pattern —
`agentProvider` (`@allowed(['copilot','claude'])`, default `'copilot'`) and `anthropicApiKey`
(`@secure()`, default `''`). Wire into the existing Container App secrets array
(`{ name: 'anthropic-api-key', value: anthropicApiKey }`) and the agent container's env array
(`{ name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }`,
`{ name: 'AGENT_PROVIDER', value: agentProvider }`), alongside the existing `GITHUB_TOKEN`/
`SANDBOX_PROVIDER` entries. Run `az bicep build --file infra/main.bicep --stdout` to confirm it
still compiles (a pre-existing unrelated warning about a hardcoded DB URL is expected and fine —
do not fix it, out of scope).

Self-check: N/A, covered by Task 12's import verification plus the bicep build.

---

## Task 12: Final verification sweep

**Files:** none changed — this task only runs checks and writes a report. Depends on all of
Tasks 1-11.

1. From `agent/`: `.venv/Scripts/python.exe -m py_compile` every `.py` file touched across all
   eleven prior tasks in one command.
2. Full-app import under both providers (the Global Constraints command block), plus every
   individual new/rewritten module's own `_demo()`:
   `python -m src.cli_agent_exec`, `python -m src.claude_chat_model`,
   `python -m src.copilot_chat_model`, `python -m src.chat_model`, `python -m src.structured_output`
   (adjust module names to whatever Tasks 1-4 actually produced).
3. `az bicep build --file infra/main.bicep --stdout` (repeat Task 11's check as a final gate).
4. **Real container build**: `docker build -t ai-dev-workflow-sandbox:test agent/sandbox-image/`
   from the repo root. If this fails, report the exact error — do not attempt to work around a
   Dockerfile syntax problem by guessing; that's a defect in Task 7's work to fix, not something
   to paper over here.
5. **Real container run, both providers** — this is where every "verify at implementation" flag
   from Tasks 2/3/6 gets resolved against reality, not left as a guess:
   - Start a container from the built image with `AGENT_PROVIDER=claude` and a real (or
     test/sandboxed) `ANTHROPIC_API_KEY`, confirm `wait_for_cli_ready`'s `claude --version` check
     passes, then exec one real `claude -p` turn through the actual `cli_agent_exec.run_turn` path
     against a trivial prompt in `/workspace/repo` — confirm the JSON result parses as Task 2
     expects and a `session_id` comes back usable for `--resume` on a second turn.
   - Same with `AGENT_PROVIDER=copilot` and a real `GITHUB_TOKEN` — this is the important one:
     capture the **actual raw JSONL** `copilot -p --output-format=json` produces, confirm or fix
     Task 3's per-line parser against real output, and confirm `--session-id` resume actually
     works. Report the raw sample in full — this is genuinely new information nothing in this
     session verified yet.
   - For whichever provider has real credentials available in this environment, additionally
     confirm: a killed/timed-out turn's session is resumable (or cleanly fails, not silently
     confused) on the next `--resume` attempt, and whichever `read_skill_invocations` path is
     testable actually finds a real skill invocation's log entry.
6. Write a final report naming, explicitly: what was verified for real against a running
   container, what remains unverified and why (e.g. no credentials available for one provider in
   this environment), and any place reality diverged from what Tasks 1-11 assumed — with the
   specific file/line that needs a follow-up fix if so.

No `_demo()` needed for this task — it produces a report, not code.
