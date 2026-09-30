<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# No hardcoded magic numbers or strings in `agent/src/**`

A runtime-tunable value (a char/line/item cap, a timeout, a threshold, a retry count, a
sandbox-relative path, a format/allowlist) belongs in `agent/src/config.py`'s declarative
`_SETTINGS` table — live-editable via the DB-backed Org Settings system, not a bare
`NAME = int(os.environ.get(...))` line, and not scattered into whichever file happens to use the
value. Never hand-write a new module-level `os.environ.get(...)`-backed constant outside
`config.py`, and never re-export/snapshot one of `config.py`'s settings into another module's own
constant (a plain module-level assignment reads the value once, at import time, and then never
sees a live override again — see the "stays live, never snapshotted" rule below).

Adding a new setting means adding one entry to `config.py`'s `_SETTINGS` dict:

- A `_Setting(parser, env_var, default_raw, category, purpose, effect, value_range)` — the
  `parser` key (`"int"`/`"float"`/`"str"`/`"bool"`/`"csv"`/`"csv_int"`/`"csv_float"`/
  `"frozenset_csv"`) must have a matching `_FORMATTERS` entry that round-trips back to an equal
  value; add a new parser/formatter pair together if the existing ones don't fit.
- `purpose`/`effect`/`value_range` are real content, not filler — the Settings UI shows them
  verbatim next to the field's edit control. `purpose`: one line, what this knob controls.
  `effect`: one line, what raising/lowering/changing it actually does, and any tradeoff.
  `value_range`: the valid range/format, or `None` if not meaningfully bounded.
- New constant, never previously an env var: prefix it `AIDW_`. Relocating a value that already
  reads its own env var today: keep that exact env var name — renaming it silently breaks anyone's
  existing `.env`/deploy config for zero functional benefit (see `MIN_COVERAGE_PERCENT` and
  `TEST_COVERAGE_REPLAY_TIMEOUT_SECONDS` in `config.py` for both cases).
- If several call sites already share one literal for the same purpose, give them ONE setting, not
  one each — don't invent independent knobs for values that were never meant to move independently
  (e.g. `GIT_OPS_HTTP_TIMEOUT_SECONDS` is reused by every short-lived outbound `httpx.AsyncClient`
  in this codebase, not one setting per call site).
- A tail-only slice (`text[-N:]`) that captures output which could be a multi-item list (test
  output, build logs, findings) and feeds back into a model's next attempt is its own bug, not just
  a magic number: it silently drops the START of the list every time. Use
  `agent/src/text_truncate.py`'s `truncate_middle(text, head_chars, tail_chars)` instead, sized
  from two settings (a `_HEAD_CHARS`/`_TAIL_CHARS` pair), so both ends survive.

**Every call site reads the setting live, never snapshots it.** `config.NAME` /
`workflow_config.NAME` resolves dynamically via `config.py`'s own module `__getattr__` (PEP 562)
— this session's DB-backed override if one is pinned, else the env/default fallback. That means:

- Read it at the point of use (inside a function body), never bake it into a module-level global,
  a function's default-argument value, a dataclass field default, or any other structure built
  once at import/def time — those snapshot whatever the value was when the process started and
  never see a later override. If the value feeds a module-level collection built once at import
  (e.g. a list of dataclass instances, a static command-string tuple), wrap the field in a
  zero-arg `Callable`/defer it to a function called at point of use instead of a plain value —
  `graph.py`'s `StageSpec.max_cycles`/`max_verify_cycles` and `repo_scan.py`'s `TOOLS`/
  `_build_jscpd_command` are the established patterns for a dataclass field and a static
  command-string tuple respectively.
  - Corollary for import-time re-exports: `some_module._NAME = other_module.SOME_SETTING` at
    module scope has the identical bug (and breaks entirely once the source constant moves into
    `config.py` — there is no more bare attribute to re-export). Import `config` directly and read
    `config.NAME` at point of use instead.
- Never do a plain `config.NAME = value` assignment (including in a test/self-check) to stub or
  override a setting — that creates a real, permanent module-`__dict__` entry that silently and
  irreversibly defeats `__getattr__`-based live resolution for that name for the rest of the
  process's life. Use `unittest.mock.patch.object(config, "NAME", value)` instead, which tears
  down correctly (`delattr`s on exit, restoring dynamic resolution).
- The one exception: a value read before any session is pinned (e.g. `run_lock.py`'s
  `acquire_run_lock`, called before the event loop starts) always resolves env/default and can
  never see a live override — document that explicitly on the setting and at the call site rather
  than silently letting it look live when it isn't.

**Sandbox-mirrored leaf modules are a separate case, not exempt.** A handful of files
(`agent/src/gates/coverage_parsing.py`, `test_quality_checks.py`, `wireframe_linkage_checks.py`,
and others under `agent/sandbox-image/hooks/`) are deliberately dependency-free and byte-mirrored
into the sandbox image — they must never import `config.py` (or anything else project-local), so
their own `os.environ.get(...)`-backed constants stay exactly as they are today, outside
`_SETTINGS`. Any operator-tunable value in one of these files still needs a deploy operator's
override to actually reach the sandboxed container: add (or extend) the container-boot env-var
passthrough in `agent/src/sandbox/local_docker.py` and `azure_aci.py` (search for "Final-review
Fix Round 2, Item 5") using a literal copy of that file's own default — do not assume adding the
constant alone is sufficient, since the host's `os.environ` never automatically reaches a separate
container.

**Not everything that looks like a magic string belongs in `config.py`.** Its whole point is
*operator-tunable runtime knobs* — something a person deploying this agent might reasonably want
to change without editing code. That excludes:

- Internal state-machine/status tags compared only within the file that sets them (e.g.
  `rebuild.py`'s `rb["status"]` values `"clean"/"failed"/"fixing"`) — not operator settings, and
  grepping confirms no other file reads them.
- Sentinel/reason-code constants already named and imported by reference elsewhere (e.g.
  `test_coverage_gate.py`'s `REASON_*` constants) — callers import the name, never retype the
  literal, so there's no duplication risk to fix.
- Prompt-name identifiers passed to `load_prompt_pair`/`load_prompt` (e.g. `"rebuild_build_fix"`)
  — 1:1 coupled to a file under `agent/src/prompts/`; moving the identifier into `config.py` while
  the prompt file itself stays hardcoded achieves nothing.
- Log/error message text, f-string templates, and docstrings.
- A real platform/OS ceiling, not a preference (e.g. Windows `CreateProcess`'s ~32767-char argv
  limit behind `EXEC_CMD_BUDGET_CHARS`) — raising it past the actual limit breaks things, so it
  isn't a tunable knob even though it's a number with a name.
- A value with no realistic operator-tunability case: a test fixture's own polling/timing
  constant, an untuned formula/weight table with zero observed retuning history, or anything else
  where a deploy operator would have no real reason to override it independent of a code change.
  Give it a normal named code constant and stop there — a number living in `config.py` doesn't
  make it more configurable if nobody would ever plausibly configure it.

Putting these in `config.py` anyway doesn't make them more configurable — it just buries an
internal protocol detail in a module whose entire contract is "safe for an operator to change."
When in doubt: would a deploy operator ever plausibly want to override this via an env var,
independent of a code change? If not, it isn't config.
