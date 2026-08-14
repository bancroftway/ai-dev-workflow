#!/usr/bin/env bash
# Sandbox container entrypoint (architecture plan Section C/C.4).
#
# Responsibilities, in order:
#   1. Clone REPO_BRANCH of the repo at REPO_CLONE_URL into /workspace, authenticating with
#      GIT_USER_TOKEN via a one-shot git credential helper -- the token is only ever passed to
#      this single `git clone` invocation (via `git -c credential.helper=...`), never written to
#      a persistent .gitconfig or long-lived env var a later process could read. Skipped entirely
#      when REPO_CLONE_URL is unset, so this image is also usable as a bare Copilot-runtime
#      sandbox for testing the transport/connect mechanics on their own.
#   2. exec the Copilot CLI runtime in headless TCP server mode, authenticated with the shared
#      COPILOT_SDK_AUTH_TOKEN (agent/src/graph.py's GITHUB_TOKEN) and gated by
#      COPILOT_CONNECTION_TOKEN. `exec` (not a backgrounded process) so this process IS pid 1's
#      replacement -- container lifecycle and signal delivery (docker stop -> SIGTERM) match the
#      copilot process directly, and `docker logs` shows its output.
#
# Ordering note (plan Section C.4): once devcontainer.json onCreateCommand/postCreateCommand
# support lands, it must run strictly after step 1's credential material is already gone and
# strictly before COPILOT_SDK_AUTH_TOKEN is relied upon by anything -- an untrusted repo's own
# postCreateCommand runs with the same privileges as this script.
set -euo pipefail

WORKSPACE_DIR="/workspace/repo"
COPILOT_SERVER_PORT="${COPILOT_SERVER_PORT:-3000}"

if [[ -n "${REPO_CLONE_URL:-}" ]]; then
  if [[ -z "${GIT_USER_TOKEN:-}" ]]; then
    echo "entrypoint: REPO_CLONE_URL set but GIT_USER_TOKEN is empty -- refusing to clone anonymously" >&2
    exit 1
  fi

  CRED_HELPER_SCRIPT="$(mktemp)"
  trap 'rm -f "$CRED_HELPER_SCRIPT"' EXIT

  cat > "$CRED_HELPER_SCRIPT" <<EOF
#!/bin/sh
echo "username=x-access-token"
echo "password=${GIT_USER_TOKEN}"
EOF
  chmod 700 "$CRED_HELPER_SCRIPT"

  echo "entrypoint: cloning ${REPO_CLONE_URL} (branch ${REPO_BRANCH:?REPO_BRANCH is required when REPO_CLONE_URL is set}) into ${WORKSPACE_DIR}"
  git -c credential.helper="$CRED_HELPER_SCRIPT" \
    clone --branch "$REPO_BRANCH" --single-branch "$REPO_CLONE_URL" "$WORKSPACE_DIR"

  # The pipeline never commits on the user's selected branch: it works on a tool-owned branch
  # named ai-dev-workflow/<selected-branch>. If that work branch already exists on origin (a
  # brownfield re-entry), check it out so prior .ai-dev-workflow/ artifacts hydrate; otherwise
  # branch off the fresh clone. Must run BEFORE the credential material is destroyed below --
  # the existence probe and fetch both need auth. `git ls-remote --exit-code` guards the fetch:
  # a plain fetch of a missing ref exits non-zero and would kill the container under `set -e`.
  WORK_BRANCH="ai-dev-workflow/${REPO_BRANCH}"
  if git -C "$WORKSPACE_DIR" -c credential.helper="$CRED_HELPER_SCRIPT" \
      ls-remote --exit-code origin "refs/heads/${WORK_BRANCH}" >/dev/null 2>&1; then
    echo "entrypoint: work branch ${WORK_BRANCH} exists on origin -- checking it out"
    git -C "$WORKSPACE_DIR" -c credential.helper="$CRED_HELPER_SCRIPT" \
      fetch origin "+refs/heads/${WORK_BRANCH}:refs/remotes/origin/${WORK_BRANCH}"
    git -C "$WORKSPACE_DIR" checkout -b "$WORK_BRANCH" "origin/${WORK_BRANCH}"
  else
    echo "entrypoint: creating work branch ${WORK_BRANCH} off ${REPO_BRANCH}"
    git -C "$WORKSPACE_DIR" checkout -b "$WORK_BRANCH"
  fi

  rm -f "$CRED_HELPER_SCRIPT"
  trap - EXIT
  unset GIT_USER_TOKEN

  cd "$WORKSPACE_DIR"

  # Toolchain bootstrap runs here specifically: after the credential material is gone (it acts on
  # repo-supplied content, and must never see the token) and before the Copilot runtime is exec'd
  # (so a repo's pinned Node/.NET version is already on PATH for every later build and test).
  # Non-fatal by design -- see bootstrap.sh's own header.
  ai-dev-workflow-bootstrap.sh "$WORKSPACE_DIR" || \
    echo "entrypoint: bootstrap reported a failure -- continuing (a missing toolchain surfaces as a real build error later)" >&2
else
  echo "entrypoint: REPO_CLONE_URL not set -- skipping clone, starting a bare sandbox"
  mkdir -p "$WORKSPACE_DIR"
  cd "$WORKSPACE_DIR"
fi

if [[ -z "${COPILOT_SDK_AUTH_TOKEN:-}" ]]; then
  echo "entrypoint: WARNING -- COPILOT_SDK_AUTH_TOKEN is empty; the copilot runtime will start" \
       "but any session creation will fail auth" >&2
fi

# --host 0.0.0.0 is required, not cosmetic: copilot's TCP server rejects any connection whose
# peer isn't true loopback by default (confirmed empirically -- a Docker-published connection to
# the default bind arrives as the bridge gateway's address, not 127.0.0.1, and gets destroyed
# before even reaching the JSON-RPC layer, with zero response). --host 0.0.0.0 is GitHub's own
# documented mechanism for exactly this case ("from another machine on your network" / "from a
# separate process or container") -- COPILOT_CONNECTION_TOKEN is still what gates access once
# bound this way (verified: a wrong token gets a clean AUTHENTICATION_FAILED JSON-RPC error over
# the published port, not a silent drop). Because the wire protocol is plaintext TCP with no TLS,
# this still relies on the surrounding network being trusted (loopback-only publish for local
# dev, internal-only Container Apps ingress in Azure -- see architecture plan Section D) rather
# than on the token alone.
echo "entrypoint: starting copilot --server on 0.0.0.0:${COPILOT_SERVER_PORT}"
exec copilot \
  --headless \
  --no-auto-update \
  --log-level error \
  --auth-token-env COPILOT_SDK_AUTH_TOKEN \
  --no-auto-login \
  --host 0.0.0.0 \
  --port "$COPILOT_SERVER_PORT"
