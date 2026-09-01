#!/usr/bin/env bash
# Full git-history secret scan. Usage: gitleaks.sh [repo-dir, default .]
# Docker-based so a dev's machine never needs gitleaks installed -- same image/flags as CI.
set -euo pipefail
export MSYS_NO_PATHCONV=1 # Git-Bash-on-Windows mangles container-side paths in -v otherwise.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/versions.sh"

REPO_DIR="$(cd "${1:-.}" && pwd)"
BASELINE_ARGS=()
[ -f "$REPO_DIR/.gitleaks-baseline.json" ] && BASELINE_ARGS=(--baseline-path /repo/.gitleaks-baseline.json)

docker run --rm -v "$REPO_DIR:/repo" "$GITLEAKS_IMAGE" \
  git --redact --exit-code 1 "${BASELINE_ARGS[@]}" /repo
