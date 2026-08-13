# Sandbox image: Agent Plugin content

This directory builds the per-session sandbox container that runs GitHub Copilot CLI headless.
`plugins/` is baked into the image (`Dockerfile`'s `COPY plugins/ /opt/ai-dev-workflow-plugins/`)
and is also a local Claude Code marketplace, so what you dogfood in Claude Code is byte-identical
to what ships into the sandbox.

```
plugins/
  .claude-plugin/marketplace.json   # local marketplace root
  ai-dev-workflow/                  # first-party skill pack (this repo's own)
  vendor/<source-slug>/<name>/      # vendored 3rd-party skill packs
  vendor/vendor-lock.json           # pin manifest for vendored packs
```

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

1. Target: `plugins/vendor/<source-slug>/<plugin-name>/`.
2. Copy only `.claude-plugin/plugin.json`, `commands/`, `skills/`, `.mcp.json` -- strip/reject
   `agents/`, `hooks/`, `hooks.json`, LSP config. **Never patch the vendored files** -- if a
   vendored skill's own instructions try to write files to a hardcoded path, that's neutralized
   at the calling stage's harness level (an `available_tools` allowlist + a prompt-level override
   instruction), not by editing the skill's prose.
3. Record in `plugins/vendor/vendor-lock.json` (name, source repo URL, ref/tag, commit sha, date
   vendored, vendored by).
4. Register as a `marketplace.json` entry and append its in-container path to
   `agent/src/config.py`'s `COPILOT_PLUGIN_DIRECTORIES`.
5. Re-verify via the spike technique below before merging.

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
