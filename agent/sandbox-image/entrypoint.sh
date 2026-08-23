#!/usr/bin/env bash
# Sandbox container entrypoint (architecture plan Section C/C.4).
#
# Responsibilities, in order:
#   1. Clone (or reuse -- /workspace may be a persistent named volume) REPO_BRANCH of the repo at
#      REPO_CLONE_URL into /workspace/repo, authenticating with the clone credential via a
#      one-shot git credential helper. The credential arrives either as a pre-start file
#      (~/.aidw-git-token, local Docker provider -- never in Config.Env, so `docker inspect`
#      shows nothing) or as the GIT_USER_TOKEN env var (Azure ACI secure env; env wins when both
#      are present). Either way it is only ever passed per-invocation via
#      `git -c credential.helper=...`, never written to a persistent .gitconfig, and both copies
#      are destroyed before anything repo-supplied can run. Skipped entirely when REPO_CLONE_URL
#      is unset, so this image is also usable as a bare sandbox for exercising either CLI
#      directly, without a target repo.
#   2. exec `sleep infinity` so this process (still pid 1) simply holds the container open. Nothing
#      long-lived runs in here for either provider: both Claude and Copilot are driven by a
#      per-turn CLI exec from outside the container (agent/src/sandbox/provider.py's
#      wait_for_cli_ready, plus claude_chat_model.py/copilot_chat_model.py), never by a persistent
#      in-container server. `exec` (not a backgrounded `sleep`) still matters -- it keeps this
#      script as pid 1's replacement so `docker stop`/ACI's equivalent deliver SIGTERM directly to
#      it instead of to a wrapper shell that would have to relay the signal.
#
# Ordering note (plan Section C.4): once devcontainer.json onCreateCommand/postCreateCommand
# support lands, it must run strictly after step 1's credential material is already gone and
# strictly before the active provider's own credential (COPILOT_GITHUB_TOKEN or ANTHROPIC_API_KEY,
# whichever AGENT_PROVIDER selects) is relied upon by anything -- an untrusted repo's own
# postCreateCommand runs with the same privileges as this script.
set -euo pipefail

WORKSPACE_DIR="/workspace/repo"

# Local Docker provider delivers the clone credential as a pre-start file (never in Config.Env,
# so `docker inspect` shows nothing); Azure ACI delivers it as a secure env var. Env wins when
# both are present. The file is deleted unconditionally, clone or no clone.
GIT_TOKEN_FILE="$HOME/.aidw-git-token"
if [[ -z "${GIT_USER_TOKEN:-}" && -f "$GIT_TOKEN_FILE" ]]; then
  GIT_USER_TOKEN="$(cat "$GIT_TOKEN_FILE")"
fi
rm -f "$GIT_TOKEN_FILE"

if [[ -n "${REPO_CLONE_URL:-}" ]]; then
  if [[ -z "${GIT_USER_TOKEN:-}" ]]; then
    echo "entrypoint: REPO_CLONE_URL set but no clone credential (env or token file) -- refusing to clone anonymously" >&2
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

  : "${REPO_BRANCH:?REPO_BRANCH is required when REPO_CLONE_URL is set}"

  # The pipeline never commits on the user's selected branch: it works on this session's own
  # unique work branch (ai-dev-workflow/<session_id>, agent/src/branch_naming.py), computed once by
  # the agent and passed in via this env var -- never derived here. One branch per session means
  # this session is the branch's only writer, so git_ops.push_head is a plain --force.
  : "${WORK_BRANCH:?WORK_BRANCH is required when REPO_CLONE_URL is set}"

  # /workspace may be a reused named volume: a prior session's clone (reuse it), a corrupt or
  # partial clone (nuke and re-clone), or empty (fresh clone). Reuse is best-effort end-to-end;
  # re-clone is the correctness backstop -- a volume must never be able to crash-loop the
  # container. A reused clone may hold uncommitted droppings from a killed run; the pipeline's
  # own reset-hard paths handle dirty trees, so no `git clean` here.
  if [[ -e "$WORKSPACE_DIR" ]] && ! git -C "$WORKSPACE_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "entrypoint: $WORKSPACE_DIR exists but is not a valid git work tree -- removing"
    rm -rf "$WORKSPACE_DIR"
  fi

  if [[ -d "$WORKSPACE_DIR/.git" ]]; then
    echo "entrypoint: reusing existing clone in $WORKSPACE_DIR"
    rm -f "$WORKSPACE_DIR/.git/index.lock"  # stale lock from a container killed mid-commit
    if ! git -C "$WORKSPACE_DIR" -c credential.helper="$CRED_HELPER_SCRIPT" \
        fetch origin "+refs/heads/${REPO_BRANCH}:refs/remotes/origin/${REPO_BRANCH}"; then
      echo "entrypoint: fetch on reused clone failed -- re-cloning"
      rm -rf "$WORKSPACE_DIR"
    fi
  fi

  if [[ ! -d "$WORKSPACE_DIR/.git" ]]; then
    echo "entrypoint: cloning ${REPO_CLONE_URL} (branch ${REPO_BRANCH}) into ${WORKSPACE_DIR}"
    git -c credential.helper="$CRED_HELPER_SCRIPT" \
      clone --branch "$REPO_BRANCH" --single-branch "$REPO_CLONE_URL" "$WORKSPACE_DIR"
  fi

  # Work-branch setup. Must run BEFORE the credential material is destroyed below -- the
  # existence probe and fetch both need auth. `git ls-remote --exit-code` guards the fetch: a
  # plain fetch of a missing ref exits non-zero and would kill the container under `set -e`.
  #
  # This session's own unique work branch -- origin is still treated as the source of truth (not
  # "whichever local copy is newest") because a human can merge-and-delete this branch out from
  # under a still-resumable session between runs, or this container can be recreated against a
  # reused workspace volume that's behind. Any failure in the reuse-side checkout falls back to a
  # full re-clone rather than crash-looping the volume.
  if ! (
    if git -C "$WORKSPACE_DIR" -c credential.helper="$CRED_HELPER_SCRIPT" \
        ls-remote --exit-code origin "refs/heads/${WORK_BRANCH}" >/dev/null 2>&1; then
      echo "entrypoint: fetching origin's copy of work branch ${WORK_BRANCH}"
      git -C "$WORKSPACE_DIR" -c credential.helper="$CRED_HELPER_SCRIPT" \
        fetch origin "+refs/heads/${WORK_BRANCH}:refs/remotes/origin/${WORK_BRANCH}"

      if git -C "$WORKSPACE_DIR" merge-base --is-ancestor \
          "origin/${WORK_BRANCH}" "origin/${REPO_BRANCH}"; then
        # Post-merge reset: the work branch's PR was merged (its tip is already contained in the
        # base branch), so keeping it around would re-surface already-merged commits in every
        # later PR. Operational assumption: GitHub auto-delete-branch-on-merge is enabled, so
        # origin normally won't even have this ref anymore by the time we get here -- this is the
        # defensive path for when it still does (or auto-delete is off).
        #
        # Guarded the same way the sibling (not-merged) branch below guards its reset: only safe
        # to discard the local branch when local has nothing beyond origin's (already-merged)
        # copy. A prior push can fail (log-and-continue) after this session committed locally but
        # before the PR merged upstream -- resetting unconditionally here would silently destroy
        # that unpushed work the next time this container is recreated.
        if git -C "$WORKSPACE_DIR" show-ref --verify --quiet "refs/heads/${WORK_BRANCH}" && \
            ! git -C "$WORKSPACE_DIR" merge-base --is-ancestor \
              "refs/heads/${WORK_BRANCH}" "origin/${WORK_BRANCH}"; then
          echo "entrypoint: work branch ${WORK_BRANCH} is merged upstream but has unpushed local commits -- keeping local instead of resetting"
          git -C "$WORKSPACE_DIR" checkout "$WORK_BRANCH"
        else
          echo "entrypoint: work branch ${WORK_BRANCH} is already merged into ${REPO_BRANCH} -- recreating from ${REPO_BRANCH}"
          git -C "$WORKSPACE_DIR" checkout -B "$WORK_BRANCH" "origin/${REPO_BRANCH}"
        fi
      elif git -C "$WORKSPACE_DIR" show-ref --verify --quiet "refs/heads/${WORK_BRANCH}"; then
        echo "entrypoint: work branch ${WORK_BRANCH} exists locally -- reconciling with origin"
        git -C "$WORKSPACE_DIR" checkout "$WORK_BRANCH"
        if git -C "$WORKSPACE_DIR" merge-base --is-ancestor HEAD "origin/${WORK_BRANCH}"; then
          # Origin is at or ahead of local -- this session's own branch, but a human (or a stale
          # push from before this container was recreated) moved origin further than this volume's
          # own copy since we last saw it. Local has nothing origin lacks, so this is always a
          # fast-forward.
          git -C "$WORKSPACE_DIR" reset --hard "origin/${WORK_BRANCH}"
        fi
        # Else: local is ahead of origin (this session's own unpushed commits) -- keep local, the
        # next push carries them.
      else
        echo "entrypoint: work branch ${WORK_BRANCH} exists on origin -- checking it out"
        git -C "$WORKSPACE_DIR" checkout -b "$WORK_BRANCH" "origin/${WORK_BRANCH}"
      fi
    elif git -C "$WORKSPACE_DIR" show-ref --verify --quiet "refs/heads/${WORK_BRANCH}"; then
      git -C "$WORKSPACE_DIR" checkout "$WORK_BRANCH"
    else
      # Base on the freshly fetched REPO_BRANCH, never whatever HEAD a previous session left
      # checked out (which may be a stale work branch, or REPO_BRANCH from a since-changed
      # PR target).
      echo "entrypoint: creating work branch ${WORK_BRANCH} off ${REPO_BRANCH}"
      git -C "$WORKSPACE_DIR" checkout -b "$WORK_BRANCH" "origin/${REPO_BRANCH}"
    fi
  ); then
    echo "entrypoint: work-branch setup failed on reused clone -- re-cloning from scratch"
    rm -rf "$WORKSPACE_DIR"
    git -c credential.helper="$CRED_HELPER_SCRIPT" \
      clone --branch "$REPO_BRANCH" --single-branch "$REPO_CLONE_URL" "$WORKSPACE_DIR"
    git -C "$WORKSPACE_DIR" checkout -b "$WORK_BRANCH" "origin/${REPO_BRANCH}"
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

# One shape for every provider: clone, bootstrap, exec sleep infinity. Neither CLI is started
# here -- both are driven by a per-turn `docker exec`/`az container exec` from outside (see this
# file's own header comment, responsibility #2) -- so the only provider-specific thing left is
# which credential gets warned about when empty. An unrecognized AGENT_PROVIDER value falls
# through to the copilot-shaped warning below rather than crashing the container here;
# chat_model.py's own dispatch already raises ValueError the moment a session actually tries to
# use it, which is the right layer to fail loudly at, not this script.
AGENT_PROVIDER="${AGENT_PROVIDER:-copilot}"
if [[ "$AGENT_PROVIDER" == "claude" ]]; then
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "entrypoint: WARNING -- ANTHROPIC_API_KEY is empty; the claude runtime will start" \
         "but any session creation will fail auth" >&2
  fi
else
  # COPILOT_GITHUB_TOKEN, not COPILOT_SDK_AUTH_TOKEN (task-12-report.md BUG B) and not plain
  # GITHUB_TOKEN either (task-12b fix-round-1): this warning used to check the old SDK-server
  # process's env var, which the real `copilot` CLI (v1.0.79) never reads at all -- it reads
  # COPILOT_GITHUB_TOKEN/GH_TOKEN/GITHUB_TOKEN (see local_docker.py/azure_aci.py's own fix for the
  # same root cause). COPILOT_GITHUB_TOKEN specifically, not plain GITHUB_TOKEN: `gh` (installed in
  # this image) and git credential helpers auto-authenticate from a plain GITHUB_TOKEN/GH_TOKEN
  # with zero extra config, which would silently hand the shared fleet PAT to `gh`/git/any
  # repo-supplied script under a turn already running --no-ask-user -- exactly the ambient
  # long-lived credential exposure this image's own credential handling elsewhere (the one-shot git
  # token file above, destroyed before repo content runs) is designed to avoid. Left checking a
  # stale name, this warning would stay silent on a genuinely-empty COPILOT_GITHUB_TOKEN and could
  # fire falsely if some other name alone were ever set -- same symptom as the bug itself, not a
  # separate one.
  if [[ -z "${COPILOT_GITHUB_TOKEN:-}" ]]; then
    echo "entrypoint: WARNING -- COPILOT_GITHUB_TOKEN is empty; the copilot runtime will start" \
         "but any session creation will fail auth" >&2
  fi
fi

exec sleep infinity
