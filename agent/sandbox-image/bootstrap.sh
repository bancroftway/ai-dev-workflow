#!/usr/bin/env bash
# Per-repo toolchain bootstrap (called by entrypoint.sh, after the clone and after the git
# credential material has been destroyed -- the ordering entrypoint.sh's own header prescribes for
# anything that acts on repo-supplied content).
#
# What this is for: the sandbox image is immutable and deliberately small, so it cannot ship every
# toolchain every repo needs. This installs the ones a repo *declares for itself* into /opt/aidw,
# outside the source tree, and records what it found so the image can be improved from evidence
# rather than guesswork.
#
# Three rules this script exists to keep:
#   1. Nothing is written into the repo. Toolchains go to /opt/aidw/tools; the only file touched
#      under the clone is .git/info/exclude, which is local-only and never committed.
#   2. Only repo-declared versions are installed, and only from mise's own tool registry. A
#      mise.toml naming an arbitrary asdf plugin git URL is third-party shell that would run here,
#      automatically, before anything else in the container -- refused and recorded instead.
#   3. Failure is never fatal. A missing toolchain surfaces later as a real build error with a real
#      message, which is strictly more useful than a container that refuses to start.
set -uo pipefail

WORKSPACE_DIR="${1:-/workspace/repo}"
REPORT_PATH="${WORKSPACE_DIR}/agent-work/toolchain-bootstrap.json"
INSTALL_TIMEOUT_SECONDS="${AIDW_BOOTSTRAP_TIMEOUT:-600}"

cd "$WORKSPACE_DIR" 2>/dev/null || { echo "bootstrap: no workspace at ${WORKSPACE_DIR} -- skipping"; exit 0; }

# ── 1. Keep tool output out of the repo's git status ──────────────────────────────────────────
# .git/info/exclude, not .gitignore: it is local to this clone, never committed, and never edits a
# file the repo actually tracks.
# Deliberately NOT excluding bin/ or obj/: `git add -- <path>` fails on an ignored path without
# -f, so excluding them would break git_ops.commit_paths for a repo that genuinely keeps source
# under bin/.
if [[ -d .git ]]; then
  mkdir -p .git/info
  touch .git/info/exclude
  for pattern in "node_modules/" ".venv/" ".mise/" "agent-work/"; do
    # grep-then-append: this script runs on every session, and an unconditional append would grow
    # the file without bound.
    grep -qxF "$pattern" .git/info/exclude || echo "$pattern" >> .git/info/exclude
  done
fi

mkdir -p "$(dirname "$REPORT_PATH")"

tools_json=""
record() { # key requested source in_image installed error
  local entry
  entry=$(printf '"%s":{"requested":"%s","source":"%s","in_image":%s,"installed":%s,"error":"%s"}' \
    "$1" "$2" "$3" "$4" "$5" "${6//\"/\'}")
  tools_json="${tools_json:+${tools_json},}${entry}"
}

# ── 2. Repo-declared toolchains ───────────────────────────────────────────────────────────────
MISE_FILE=""
for candidate in mise.toml .mise.toml .tool-versions .nvmrc .node-version; do
  [[ -f "$candidate" ]] && { MISE_FILE="$candidate"; break; }
done

if [[ -n "$MISE_FILE" ]]; then
  # Registry-only. A plugin/git URL in the config means the repo is asking us to execute code from
  # somewhere mise does not vouch for -- refuse rather than run it.
  if grep -qE '^\s*\[plugins\]|(git|https?)://.*\.git' "$MISE_FILE" 2>/dev/null; then
    echo "bootstrap: ${MISE_FILE} references a non-registry plugin source -- refusing (registry tools only)" >&2
    record "mise" "$(head -c 200 "$MISE_FILE" | tr '\n' ' ')" "$MISE_FILE" "false" "false" "non-registry plugin source refused"
  else
    echo "bootstrap: installing toolchains declared in ${MISE_FILE}"
    start=$SECONDS
    if timeout "$INSTALL_TIMEOUT_SECONDS" mise install --yes >/tmp/mise-install.log 2>&1; then
      echo "bootstrap: mise install completed in $((SECONDS - start))s"
      record "mise" "$(tr '\n' ' ' < "$MISE_FILE" | head -c 200)" "$MISE_FILE" "false" "true" ""
    else
      echo "bootstrap: mise install failed (non-fatal) -- see /tmp/mise-install.log" >&2
      record "mise" "$(tr '\n' ' ' < "$MISE_FILE" | head -c 200)" "$MISE_FILE" "false" "false" "$(tail -c 300 /tmp/mise-install.log | tr '\n' ' ')"
    fi
  fi
fi

# global.json pins a .NET SDK version, and mise does not read it (nor is .NET one of mise's core
# tools) -- so it gets its own branch via Microsoft's own installer, into the same prefix.
if [[ -f global.json ]]; then
  sdk_version=$(grep -oE '"version"\s*:\s*"[^"]+"' global.json | head -1 | grep -oE '[0-9][^"]*')
  installed_version=$(dotnet --version 2>/dev/null || echo "")
  if [[ -n "$sdk_version" && "$installed_version" != "$sdk_version"* ]]; then
    echo "bootstrap: global.json pins .NET SDK ${sdk_version} (image has ${installed_version:-none}) -- installing"
    start=$SECONDS
    if curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
       && timeout "$INSTALL_TIMEOUT_SECONDS" bash /tmp/dotnet-install.sh \
            --version "$sdk_version" --install-dir "${AIDW_TOOLS_DIR:-/opt/aidw/tools}/dotnet" \
            >/tmp/dotnet-install.log 2>&1; then
      export PATH="${AIDW_TOOLS_DIR:-/opt/aidw/tools}/dotnet:$PATH"
      echo "bootstrap: .NET SDK ${sdk_version} installed in $((SECONDS - start))s"
      record "dotnet" "$sdk_version" "global.json" "false" "true" ""
    else
      echo "bootstrap: .NET SDK ${sdk_version} install failed (non-fatal)" >&2
      record "dotnet" "$sdk_version" "global.json" "false" "false" "$(tail -c 300 /tmp/dotnet-install.log 2>/dev/null | tr '\n' ' ')"
    fi
  elif [[ -n "$sdk_version" ]]; then
    record "dotnet" "$sdk_version" "global.json" "true" "true" ""
  fi
fi

# ── 3. Report ─────────────────────────────────────────────────────────────────────────────────
# Read back by preflight_nodes.record_toolchain, which folds it into the ledger, manifest.json and
# the host-side log. Written even when empty: "we looked and the image already had everything" is
# a different fact from "bootstrap never ran", and only this file can tell them apart.
printf '{"image":"%s","tools":{%s}}\n' "${AIDW_IMAGE_REF:-unknown}" "$tools_json" > "$REPORT_PATH"
echo "bootstrap: wrote ${REPORT_PATH}"
exit 0
