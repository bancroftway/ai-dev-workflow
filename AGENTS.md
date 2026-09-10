<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# No hardcoded magic numbers or strings in `agent/src/**`

A runtime-tunable value (a char/line/item cap, a timeout, a threshold, a retry count, a
sandbox-relative path, a format/allowlist) belongs in `agent/src/config.py`, not as a bare literal
at its call site. Follow the file's existing convention:

- `SCREAMING_SNAKE_CASE`, `os.environ.get(...)`-backed (or `float(...)`/`int(...)`/`tuple(...)`
  wrapping it for a non-string value).
- One comment per constant covering **purpose** (what it bounds/controls), **where it's used**
  (file:function, or "read by X and Y too" if more than one file imports it), and **effect of
  changing it** (what raising/lowering it actually does, and any tradeoff).
- New constant, never previously an env var: prefix it `AIDW_`. Relocating a value that already
  reads its own env var today: keep that exact env var name — renaming it silently breaks anyone's
  existing `.env`/deploy config for zero functional benefit (see `MIN_COVERAGE_PERCENT` and
  `TEST_COVERAGE_REPLAY_TIMEOUT_SECONDS` in `config.py` for both cases).
- If several call sites already share one literal for the same purpose, give them ONE constant,
  not one each — don't invent independent knobs for values that were never meant to move
  independently.
- A tail-only slice (`text[-N:]`) that captures output which could be a multi-item list (test
  output, build logs, findings) and feeds back into a model's next attempt is its own bug, not just
  a magic number: it silently drops the START of the list every time. Use
  `agent/src/text_truncate.py`'s `truncate_middle(text, head_chars, tail_chars)` instead, sized
  from two config constants (a `_HEAD_CHARS`/`_TAIL_CHARS` pair), so both ends survive.

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

Putting these in `config.py` anyway doesn't make them more configurable — it just buries an
internal protocol detail in a module whose entire contract is "safe for an operator to change."
When in doubt: would a deploy operator ever plausibly want to override this via an env var,
independent of a code change? If not, it isn't config.
