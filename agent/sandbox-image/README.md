# Sandbox image: Agent Plugin content

This directory builds the per-session sandbox container that runs GitHub Copilot CLI headless.
`plugins/ai-dev-workflow/` (this project's own first-party skill pack) and `plugins/vendor/`'s
five third-party skill packs both end up at `/opt/ai-dev-workflow-plugins/` in the image, but they
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
`COPY plugins/ai-dev-workflow/ ...`). `vendor/`'s five packs are fetched from their pinned commits
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
   not JSON-driven -- five-then-six fixed plugins don't earn a generic config reader; write the
   steps as a new block matching the existing ones), then add its `destDir` to `.gitignore`
   alongside the other four.
7. Register a `marketplace.json` entry and append its in-container path to
   `agent/src/config.py`'s `COPILOT_PLUGIN_DIRECTORIES`.
8. **Verify by actually running the script**: `sh fetch-vendor-plugins.sh /tmp/vendor-verify` and
   inspect the output, or run it in place and confirm `git status` shows no unexpected changes to
   the other four plugins. Then re-verify via the spike technique below before merging.

## Confirming a plugin change actually reaches the sandbox (the "spike" smoke test)

Every finding below came from a real Docker build + a real Copilot CLI session, not
documentation-reading -- repeat this whenever a plugin-loading assumption needs re-checking:

1. Build under a throwaway tag so `DEFAULT_IMAGE`/`latest` are untouched:
   `docker build -t ai-dev-workflow-sandbox:spike agent/sandbox-image`.
2. Run it locally with a real `COPILOT_SDK_AUTH_TOKEN` (the same `GITHUB_TOKEN` used elsewhere),
   publish the Copilot port, e.g.
   `docker run -d --rm -p 18080:3000 -e COPILOT_SDK_AUTH_TOKEN=... -e COPILOT_CONNECTION_TOKEN=... -e COPILOT_SERVER_PORT=3000 ai-dev-workflow-sandbox:spike`.
3. Connect via the Python SDK directly (`RuntimeConnection.for_uri("localhost:18080", connection_token=...)`,
   `CopilotClient(connection=...)`, `client.create_session(plugin_directories=[...], ...)`) and
   send a prompt that should only succeed if the skill/tool/server actually loaded.
4. Always pair a positive test with a negative control (same prompt, the mechanism unset) to rule
   out a lucky/hallucinated match.

### Known findings from the last full spike run (do not re-derive)

- `plugin_directories` must point at the **plugin root** (containing `.claude-plugin/plugin.json`),
  not a scannable parent -- confirmed working exactly as designed.
- Tool filter entries (`available_tools`/`excluded_tools`) must be **source-qualified**:
  `"builtin:<name>"`, `"mcp:<name>"`, `"custom:<name>"` -- bare names like `"write"` are not a
  real filter target.
- **Blocklisting write-capable tools via `excluded_tools` is unsafe/incomplete.** With
  `agent_mode="autopilot"`, excluding `builtin:create` alone still let the model write via
  `builtin:bash` (shell redirection); excluding `create`+`bash` still let it through via
  `builtin:edit`; excluding all three still let it through via a fourth tool, `builtin:apply_patch`.
  **Use `available_tools` (an allowlist) for every read-only stage instead** -- confirmed to work
  cleanly: `available_tools=["builtin:view","builtin:grep","builtin:glob","builtin:task_complete","builtin:ask_user","builtin:skill"]`
  with `agent_mode="autopilot"` produced a clean refusal and the target file was genuinely never
  created (see `agent/src/config.py`'s `READ_ONLY_AVAILABLE_TOOLS`).
- `agent_mode="autopilot"` genuinely grants write access; `hooks={"on_pre_tool_use": ...}` fires
  for every attempted tool call (useful for logging/telemetry) but is not itself what blocks
  execution -- `available_tools`/`excluded_tools` do that.
- Session events expose real, per-call token usage: `SessionEventType.ASSISTANT_USAGE`
  (`input_tokens`, `output_tokens`, `reasoning_tokens`, `cache_read_tokens`, `cache_write_tokens`,
  `cost`, `duration`, `model`) and session-level `SESSION_USAGE_INFO`/`SESSION_USAGE_CHECKPOINT`
  -- no estimate/heuristic fallback needed for token-consumption tracking.
