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
# minimal `fetch` stage (which has git + coreutils and nothing else) -- five fixed plugins do not
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

echo "All vendored plugins fetched and curated under $DEST_ROOT"
