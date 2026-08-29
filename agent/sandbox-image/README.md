# Sandbox image: Agent Plugin content

This directory builds the per-session sandbox container that runs the coding-agent CLI headless
(Claude Code by default, GitHub Copilot CLI as the alternate provider).

**This image is redistributed** (SaaS + on-prem customers). Every shipped component's licence is
inventoried in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), with the licence texts baked at
`/opt/aidw/licenses/` and the semgrep rule packs pinned in `semgrep-rule-packs.txt`. Adding a
binary to the Dockerfile means adding its notices row AND its licence file to the Dockerfile's
licence step (which asserts every licence dir is non-empty at build).
`plugins/ai-dev-workflow/` (this project's own first-party skill pack) and `plugins/vendor/`'s
nine third-party skill packs both end up at `/opt/ai-dev-workflow-plugins/` in the image, but they
get there differently:

```
plugins/
  .claude-plugin/marketplace.json      # local marketplace root
  ai-dev-workflow/                     # first-party skill pack (this repo's own) -- committed
  vendor/vendor-lock.json              # provenance record for the vendored packs -- committed
  vendor/fetch-vendor-plugins.sh       # fetches + curates them at Docker build time -- committed
  vendor/wrappers/<source-slug>/       # authored .claude-plugin/plugin.json for packs whose
                                        # source ships none -- committed (tiny, not vendored)
  vendor/<source-slug>/<name>/         # the fetched packs themselves -- NOT committed, see
                                        # .gitignore; Dockerfile's `fetch` stage runs
                                        # fetch-vendor-plugins.sh to (re)populate this at build time
```

`ai-dev-workflow/` is copied into the image directly (`Dockerfile`'s
`COPY plugins/ai-dev-workflow/ ...`). `vendor/`'s nine packs are fetched from their pinned commits
and curated (stripped files, patched paths, extra license files -- see `vendor-lock.json`) by
`fetch-vendor-plugins.sh`, run as a Docker `RUN` step, never at session runtime -- a live sandbox
session runs untrusted repos and must stay network-independent, the same reason this image already
bakes Playwright's browser and the semgrep/OSV/Trivy databases at build time instead of per-session.

`plugins/` also doubles as a local Claude Code marketplace for dogfooding (see below); that only
needs the vendored content present on disk locally, so run the fetch script once yourself first:

```
sh agent/sandbox-image/plugins/vendor/fetch-vendor-plugins.sh
```

(No argument: writes plugin content as siblings of `vendor-lock.json`, same layout it has inside
the image, so `/plugin marketplace add` below sees byte-identical content either way.)

The in-container path (`/opt/ai-dev-workflow-plugins`) must stay in sync with
`agent/src/config.py`'s `COPILOT_PLUGIN_ROOT_IN_CONTAINER`/`COPILOT_PLUGIN_DIRECTORIES` —
`copilot_chat_model.py` passes that list as `plugin_directories` on every sandboxed session.

## Adding a first-party skill

1. Invoke `skill-creator` targeting `plugins/ai-dev-workflow/skills/<name>/` directly — no
   separate "move into plugin" step.
2. Run its full draft -> test cases -> eval-viewer review -> iterate loop. The eval workspace
   lands as a sibling `<name>-workspace/` dir — already excluded by `.dockerignore`/`.gitignore`.
3. Dogfood-verify in Claude Code (already installed via the local marketplace:
   `/plugin marketplace add <repo>/agent/sandbox-image/plugins`, then
   `/plugin install ai-dev-workflow@ai-dev-workflow-local`), then verify inside a real sandbox
   using the spike technique below before relying on it in the pipeline.
4. Commit only `plugins/ai-dev-workflow/**` — never the `-workspace/`/`evals/` siblings.

## Adding a command or MCP server

- **Command**: add `plugins/ai-dev-workflow/commands/<name>.md` with required frontmatter --
  auto-discovered, no build change needed.
- **MCP server**: add/extend `plugins/ai-dev-workflow/.mcp.json`; re-run the spike technique below
  scoped to that server before trusting it, since `${CLAUDE_PLUGIN_ROOT}` templating is
  Claude-Code-specific and its Copilot CLI equivalent is unconfirmed.

## Vendoring a 3rd-party skill pack

1. Add an entry to `plugins/vendor/vendor-lock.json`: `name`, `sourceUrl`, `sourcePath`, `ref`,
   `sha` (pin to a real commit, not a floating branch), `vendoredAt`, `vendoredBy`, `destDir` (where
   it lands under `plugins/vendor/`), and `notes` explaining any judgment calls (license scoping,
   what got stripped and why).
2. Copy only `.claude-plugin/plugin.json`, `commands/`, `skills/`, `.mcp.json` from the source --
   strip/reject `agents/`, `hooks/`, `hooks.json`, LSP config, and anything interactive/unvalidated
   in a headless pipeline. List every stripped path explicitly in the entry's `stripped_paths`
   array (relative to `sourcePath`) rather than leaving it as prose a script can't act on.
3. If the source's own file needs a path rewrite to work from this image's in-container location
   (Copilot CLI does not reliably report a skill's own base directory the way Claude Code does),
   add it to the entry's `patches` array (`files`/`find`/`replace`/`reason`) -- this IS allowed,
   unlike the old rule here: a hardcoded path rewrite is a mechanical necessity, not an edit to the
   skill's own guidance. Never patch anything beyond what's needed to resolve at the right path or
   remove a reference to something `stripped_paths` just deleted.
4. If the source ships license/attribution files OUTSIDE `sourcePath` that need to travel with the
   vendored copy (e.g. a root-level `LICENSE`), list them in `extra_files` (`from`/`to`).
5. If the source has no `.claude-plugin/plugin.json` of its own, author one fresh under
   `plugins/vendor/wrappers/<source-slug>/plugin.json` (this file IS committed -- it's this
   project's own small authored content, not vendored) and set the entry's `wrapper` field to its
   path.
6. Add the new entry's clone/curate/patch steps to `fetch-vendor-plugins.sh` itself (the script is
   not JSON-driven -- a small fixed set of plugins doesn't earn a generic config reader; write the
   steps as a new block matching the existing ones), then add its `destDir` to `.gitignore`
   alongside the existing vendor lines.
7. Register a `marketplace.json` entry and append its in-container path to
   `agent/src/config.py`'s `COPILOT_PLUGIN_DIRECTORIES`.
8. **Verify by actually running the script**: `sh fetch-vendor-plugins.sh /tmp/vendor-verify` and
   inspect the output, or run it in place and confirm `git status` shows no unexpected changes to
   the other plugins. Then re-verify via the spike technique below before merging.

## Confirming a plugin change actually reaches the sandbox (the "spike" smoke test)

Doc rot fix (Phase E audit M-8): this section used to describe connecting to a persistent
`copilot --server` process over a published port via the Python SDK (`RuntimeConnection.for_uri`,
`CopilotClient`, `client.create_session(...)`). That whole mechanism was retired by Part 1's
per-turn CLI-exec rewrite -- there is no server, no port, and no SDK any more (see
`sandbox/provider.py`'s own module docstring). A "session" today is nothing but a `docker exec`
running the real `copilot`/`claude` binary once per turn, so verifying plugin content means
execing that same binary directly, the same way the real pipeline does.

Every finding below came from a real Docker build + a real CLI exec inside the container, not
documentation-reading -- repeat this whenever a plugin-loading assumption needs re-checking:

1. Build under a throwaway tag so `DEFAULT_IMAGE`/`latest` are untouched:
   `docker build -t ai-dev-workflow-sandbox:spike agent/sandbox-image`.
2. Run it with a real credential and no published port -- entrypoint.sh's steady state is
   `exec sleep infinity` (nothing ever listens on a port), and `REPO_CLONE_URL` can stay unset to
   get a bare sandbox with no target repo:
   `docker run -d --rm --name spike -e AGENT_PROVIDER=copilot -e COPILOT_GITHUB_TOKEN=... ai-dev-workflow-sandbox:spike`
   (swap in `-e AGENT_PROVIDER=claude -e ANTHROPIC_API_KEY=...` to spike the Claude side instead).
3. `docker exec` straight into it and run the CLI one-shot, pointed at the plugin directory under
   test, e.g. `docker exec spike copilot -p "<prompt>" --plugin-dir /opt/ai-dev-workflow-plugins/ai-dev-workflow`
   (Claude: `docker exec spike claude -p "<prompt>" --plugin-dir ...`) -- see
   `copilot_chat_model.py`/`claude_chat_model.py`'s own `_agenerate_inner` for the exact current
   flag set (`--available-tools`/`--excluded-tools`, `--mode`/`--permission-mode`, `--agents`/
   `--agent`, ...). No SDK, no client library, no session object to construct -- the CLI binary is
   the whole surface now. Send a prompt that should only succeed if the skill/tool/server actually
   loaded.
4. Always pair a positive test with a negative control (same prompt, the mechanism unset) to rule
   out a lucky/hallucinated match.

### Known findings from the last full spike run (do not re-derive)

- `--plugin-dir` must point at the **plugin root** (containing `.claude-plugin/plugin.json`), not a
  scannable parent -- confirmed working exactly as designed.
- Tool filter entries (`--available-tools`/`--excluded-tools` on Copilot; `--tools`/
  `--disallowedTools` on Claude) must be **source-qualified**: `"builtin:<name>"`, `"mcp:<name>"`,
  `"custom:<name>"` -- bare names like `"write"` are not a real filter target.
- **Blocklisting write-capable tools is unsafe/incomplete.** With write access granted
  (`autopilot`/`bypassPermissions`), excluding `builtin:create` alone still let the model write via
  `builtin:bash` (shell redirection); excluding `create`+`bash` still let it through via
  `builtin:edit`; excluding all three still let it through via a fourth tool, `builtin:apply_patch`.
  **Use an allowlist (`--available-tools`) for every read-only stage instead** -- confirmed to work
  cleanly (see `agent/src/config.py`'s `READ_ONLY_AVAILABLE_TOOLS`).
- There is no pre-tool-use hook on either CLI. The old SDK's `hooks={"on_pre_tool_use": ...}`
  (fired for every attempted tool call, useful for logging) has no CLI-exec equivalent for either
  provider -- both provider modules say so explicitly and defer entirely to Layer 2
  (`gates/write_scope_gate.py`'s post-hoc diff check) instead. Don't design a new mechanism around
  a pre-tool-use hook existing here; it doesn't.
- Per-call token usage no longer streams live during a session. Each turn's real usage comes back
  once, in the CLI's own terminal output at the end of that turn (Claude's `--output-format json`/
  `stream-json` terminal object's `usage`/`total_cost_usd`; Copilot's JSONL stream's own terminal
  line) -- not a live per-tool-call event the way the old SDK's `SessionEventType.ASSISTANT_USAGE`/
  `SESSION_USAGE_INFO` events worked. Still a real, measured number either way (no estimate/
  heuristic fallback needed) -- but nothing to read until the turn actually finishes.
