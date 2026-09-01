#!/usr/bin/env bash
# CVE/secret/misconfig scan of a built image.
# Usage: trivy-image.sh <image-ref> <severity-csv> [timeout, default 5m0s] [ignorefile, host path]
# Docker-based; needs the host's docker socket. scanners/ignore-unfixed are fixed here (not
# per-caller flags) so the policy itself -- not just the tool version -- can't drift between
# local and CI; only the severity floor and timeout vary (frontend/agent vs. sandbox's much
# larger package count, see deploy.yml). ignorefile is for genuinely-blocked-upstream CVEs only
# (already-latest tool, fix not yet released) -- see agent/sandbox-image/.trivyignore's own header
# for what belongs there and what doesn't.
set -euo pipefail
export MSYS_NO_PATHCONV=1 # Git-Bash-on-Windows mangles container-side paths in -v otherwise.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/versions.sh"

IMAGE_REF="$1"
SEVERITY="$2"
TIMEOUT="${3:-5m0s}"
IGNOREFILE="${4:-}"

MOUNT_ARGS=()
TRIVY_ARGS=()
if [ -n "$IGNOREFILE" ]; then
  MOUNT_ARGS+=(-v "$(cd "$(dirname "$IGNOREFILE")" && pwd)/$(basename "$IGNOREFILE"):/tmp/.trivyignore:ro")
  TRIVY_ARGS+=(--ignorefile /tmp/.trivyignore)
fi

docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v trivy-cache:/root/.cache/ \
  "${MOUNT_ARGS[@]}" \
  "$TRIVY_IMAGE" image \
  --scanners vuln,secret,misconfig \
  --severity "$SEVERITY" \
  --ignore-unfixed \
  --exit-code 1 \
  --timeout "$TIMEOUT" \
  "${TRIVY_ARGS[@]}" \
  "$IMAGE_REF"
