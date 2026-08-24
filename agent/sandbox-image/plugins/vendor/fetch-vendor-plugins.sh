#!/bin/sh
# Fetches every vendored 3rd-party skill pack listed in vendor-lock.json at its pinned commit,
# applies the exact curation (stripped files, patched paths, extra license files) vendor-lock.json
# documents, and writes the result to plugins/vendor/<source-slug>/<name>/ -- the same tree that
# used to be committed to git directly.
#
# Run at Docker BUILD time (agent/sandbox-image/Dockerfile's `fetch` stage), never at session
# runtime: a live sandbox session must stay network-independent (it runs untrusted repos), the
# same reason this image already bakes Playwright's browser and the semgrep/OSV/Trivy databases at
# build time instead of fetching them per-session. Also run locally once (`sh
# fetch-vendor-plugins.sh` from this directory) before `/plugin marketplace add` -- see this
# directory's README section on dogfooding.
#
# Usage: fetch-vendor-plugins.sh [dest-root]
#   dest-root defaults to this script's own directory (local/dev use: writes plugin content as
#   siblings of vendor-lock.json, same layout as when it was committed to git). The Dockerfile
#   passes /opt/ai-dev-workflow-plugins/vendor explicitly, so the build writes straight to its
#   final in-container location with no extra copy step. wrappers/ (authored, not fetched) is
#   always read from THIS script's own directory regardless of dest-root.
#
# Portable POSIX sh (no bash-isms): the Dockerfile's own RUN blocks all use the default `/bin/sh`,
# and this script needs to run identically there and in a contributor's local shell.
#
# vendor-lock.json stays the human-readable provenance record (who vendored what, when, why); the
# sourceUrl/ref/sha/sourcePath values below are duplicated from it as plain shell variables rather
# than JSON-parsed at build time, so this script needs no new tool installed into the Dockerfile's
# minimal `fetch` stage (which has git + coreutils and nothing else) -- nine fixed plugins do not
# earn a generic JSON-driven config reader.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST_ROOT="${1:-$SCRIPT_DIR}"
mkdir -p "$DEST_ROOT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

clone_at() {
  # clone_at <url> <sha> <dest-clone-dir>
  git clone --quiet "$1" "$WORK/$3"
  git -C "$WORK/$3" checkout --quiet "$2"
}

strip_paths() {
  # strip_paths <root> <relative-path>...
  root="$1"
  shift
  for p in "$@"; do
    rm -rf "${root:?}/${p:?}"
  done
}

write_wrapper() {
  # write_wrapper <wrapper-json-source> <dest-plugin-root>
  mkdir -p "$2/.claude-plugin"
  cp "$1" "$2/.claude-plugin/plugin.json"
}

install_plugin() {
  # install_plugin <clone-dir> <source-path-within-clone> <dest-path-under-plugins/vendor>
  # dest is the path the copy LANDS AT (not a parent to nest under) -- e.g. src_path "skills"
  # landing at ".../ponytail/skills" preserves the skills/ level the plugin root needs beside its
  # sibling .claude-plugin/, exactly like cp -r's own "dest becomes a copy of src" semantics.
  clone_dir="$1"
  src_path="$2"
  dest="$DEST_ROOT/$3"
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  cp -r "$WORK/$clone_dir/$src_path" "$dest"
}

echo "== ponytail =="
clone_at https://github.com/dietrichgebert/ponytail.git 2ed6c52c9d7e5e56942508591085fd45dea277d3 ponytail-src
install_plugin ponytail-src skills dietrichgebert-ponytail/ponytail/skills
write_wrapper "$SCRIPT_DIR/wrappers/dietrichgebert-ponytail/plugin.json" "$DEST_ROOT/dietrichgebert-ponytail/ponytail"

echo "== caveman (MIT-licensed skill only -- source repo's engine/proxy/browser components are BSL-1.1) =="
clone_at https://github.com/JuliusBrussee/caveman.git 099327780ef69ad88c4cfc15c54314579ac367a4 caveman-src
install_plugin caveman-src skills/caveman juliusbrussee-caveman/caveman/skills/caveman
write_wrapper "$SCRIPT_DIR/wrappers/juliusbrussee-caveman/plugin.json" "$DEST_ROOT/juliusbrussee-caveman/caveman"

echo "== security-review (one skill among many in a shared collection repo) =="
clone_at https://github.com/github/awesome-copilot.git 0a6e37e4e242c944380228fa29dbd14e64ac1b63 awesome-copilot-src
install_plugin awesome-copilot-src skills/security-review github-awesome-copilot/security-review/skills/security-review
write_wrapper "$SCRIPT_DIR/wrappers/github-awesome-copilot/plugin.json" "$DEST_ROOT/github-awesome-copilot/security-review"

echo "== superpowers (ships its own .claude-plugin/{plugin,marketplace}.json -- no wrapper needed) =="
clone_at https://github.com/obra/superpowers.git b36e0829c6d0140e93cfef2ca599b1b07d4a7797 superpowers-src
install_plugin superpowers-src skills obra-superpowers/superpowers/skills
mkdir -p "$DEST_ROOT/obra-superpowers/superpowers/.claude-plugin"
cp "$WORK/superpowers-src/.claude-plugin/plugin.json" "$WORK/superpowers-src/.claude-plugin/marketplace.json" \
  "$DEST_ROOT/obra-superpowers/superpowers/.claude-plugin/"

echo "== impeccable (Apache-2.0 -- LICENSE+NOTICE copied for attribution; interactive/live features stripped) =="
clone_at https://github.com/pbakaus/impeccable.git 5a149f3fdb1b5793f10567233b1dcab98fc305fd impeccable-src
install_plugin impeccable-src plugin pbakaus-impeccable/impeccable
IMP="$DEST_ROOT/pbakaus-impeccable/impeccable"
cp "$WORK/impeccable-src/LICENSE" "$IMP/LICENSE"
cp "$WORK/impeccable-src/NOTICE.md" "$IMP/NOTICE.md"
# Stripped as interactive/unvalidated in a headless pipeline (see vendor-lock.json's notes):
# the .grok-plugin (other-platform) directory, the four impeccable-* subagents, the hooks system,
# and the whole "live" visual-iteration feature (server, browser injection, per-framework adapters).
strip_paths "$IMP" \
  .grok-plugin \
  agents \
  hooks \
  skills/impeccable/reference/hooks.md \
  skills/impeccable/reference/live.md \
  skills/impeccable/reference/live-setup.md \
  skills/impeccable/scripts/hook.mjs \
  skills/impeccable/scripts/hook-admin.mjs \
  skills/impeccable/scripts/hook-before-edit.mjs \
  skills/impeccable/scripts/hook-lib.mjs \
  skills/impeccable/scripts/live.mjs \
  skills/impeccable/scripts/live-accept.mjs \
  skills/impeccable/scripts/live-browser.js \
  skills/impeccable/scripts/live-browser-dom.js \
  skills/impeccable/scripts/live-browser-session.js \
  skills/impeccable/scripts/live-commit-manual-edits.mjs \
  skills/impeccable/scripts/live-complete.mjs \
  skills/impeccable/scripts/live-copy-edit-agent.mjs \
  skills/impeccable/scripts/live-discard-manual-edits.mjs \
  skills/impeccable/scripts/live-inject.mjs \
  skills/impeccable/scripts/live-insert.mjs \
  skills/impeccable/scripts/live-manual-edit-evidence.mjs \
  skills/impeccable/scripts/live-poll.mjs \
  skills/impeccable/scripts/live-resume.mjs \
  skills/impeccable/scripts/live-server.mjs \
  skills/impeccable/scripts/live-status.mjs \
  skills/impeccable/scripts/live-target.mjs \
  skills/impeccable/scripts/live-wrap.mjs \
  skills/impeccable/scripts/live
# Every remaining reference/*.md and SKILL.md names its own scripts via a hardcoded
# `.claude/skills/impeccable/...` fallback path -- Copilot CLI does not reliably report a skill's
# base directory the way Claude Code does (see vendor-lock.json's note), so that fallback must
# resolve to where this image actually puts the skill. One find/replace, applied to every .md file
# under skills/impeccable/ (SKILL.md and every reference/*.md that has the string).
find "$IMP/skills/impeccable" -name '*.md' -exec \
  sed -i "s|\.claude/skills/impeccable|/opt/ai-dev-workflow-plugins/vendor/pbakaus-impeccable/impeccable/skills/impeccable|g" {} +
# SKILL.md's own argument-hint token, table row, and paragraph for the just-stripped `live`
# command and `hooks` sub-command must not point at files that no longer exist. All three are
# single logical lines in the source file (confirmed against a real diff against upstream), so a
# plain line-anchored sed -- no python/jq needed in this image's minimal `fetch` build stage.
sed -i \
  -e 's/extract|live\]/extract]/' \
  -e '/^| `live` | Iterate |.*reference\/live\.md.*$/d' \
  -e '/^\*\*Hooks:\*\* `\/impeccable hooks /d' \
  "$IMP/skills/impeccable/SKILL.md"

echo "== anthropics/claude-plugins-official (frontend-design, code-review, code-simplifier) =="
# security-guidance deliberately NOT fetched: it is hooks-ONLY, its SessionStart hook pip-installs
# claude-agent-sdk at session runtime (network -- a live sandbox session must stay
# network-independent), and stripping hooks (the impeccable precedent for unvalidated mechanisms)
# would leave an empty plugin. plugin-dev/skill-creator/claude-code-setup and every MCP-server
# plugin (context7, playwright, chrome-devtools-mcp, github, mintlify) skipped too -- see
# vendor-lock.json's "skipped" array for each rationale.
clone_at https://github.com/anthropics/claude-plugins-official.git 340e33aef211d95769d252324854497af871dafe cpo-src
install_plugin cpo-src plugins/frontend-design anthropics-claude-plugins-official/frontend-design
install_plugin cpo-src plugins/code-review anthropics-claude-plugins-official/code-review
install_plugin cpo-src plugins/code-simplifier anthropics-claude-plugins-official/code-simplifier

echo "== mattpocock/skills (7 skills of a larger collection -- MIT) =="
# Requested set: grill-me, grill-with-docs, diagnosing-bugs, improve-codebase-architecture -- plus
# the three support skills those four call via the Skill tool (verified in their own SKILL.md
# bodies): grilling, domain-modeling, codebase-design. tdd and to-spec deliberately skipped (see
# vendor-lock.json). Each skill ships an agents/openai.yaml (other-platform packaging) -- stripped.
# diagnosing-bugs/scripts/ and improve-codebase-architecture/HTML-REPORT.md stay: both are
# referenced from their own SKILL.md.
clone_at https://github.com/mattpocock/skills.git 6654f6b60cd9d5be8b54c6fafe44346dabeb3b76 mattpocock-src
MPS="$DEST_ROOT/mattpocock-skills/mattpocock-skills"
install_plugin mattpocock-src skills/productivity/grill-me mattpocock-skills/mattpocock-skills/skills/grill-me
install_plugin mattpocock-src skills/productivity/grilling mattpocock-skills/mattpocock-skills/skills/grilling
install_plugin mattpocock-src skills/engineering/grill-with-docs mattpocock-skills/mattpocock-skills/skills/grill-with-docs
install_plugin mattpocock-src skills/engineering/diagnosing-bugs mattpocock-skills/mattpocock-skills/skills/diagnosing-bugs
install_plugin mattpocock-src skills/engineering/improve-codebase-architecture mattpocock-skills/mattpocock-skills/skills/improve-codebase-architecture
install_plugin mattpocock-src skills/engineering/domain-modeling mattpocock-skills/mattpocock-skills/skills/domain-modeling
install_plugin mattpocock-src skills/engineering/codebase-design mattpocock-skills/mattpocock-skills/skills/codebase-design
cp "$WORK/mattpocock-src/LICENSE" "$MPS/LICENSE"
write_wrapper "$SCRIPT_DIR/wrappers/mattpocock-skills/plugin.json" "$MPS"
strip_paths "$MPS" \
  skills/grill-me/agents \
  skills/grilling/agents \
  skills/grill-with-docs/agents \
  skills/diagnosing-bugs/agents \
  skills/improve-codebase-architecture/agents \
  skills/domain-modeling/agents \
  skills/codebase-design/agents
# Upstream marks the user-invoked skills `disable-model-invocation: true` -- correct for a human
# driving a chat, fatal here: this pipeline's sessions ARE the model, headless, so a skill the
# model cannot invoke via its Skill tool is unreachable. Flip the flag on the three we prompt
# stages to use; the support skills (grilling/domain-modeling/codebase-design) and diagnosing-bugs
# are already model-invocable upstream.
for s in grill-me grill-with-docs improve-codebase-architecture; do
  sed -i 's/^disable-model-invocation: true$/disable-model-invocation: false/' "$MPS/skills/$s/SKILL.md"
done

echo "All vendored plugins fetched and curated under $DEST_ROOT"
