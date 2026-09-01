#!/usr/bin/env bash
# Full local security-check suite -- the same hadolint/gitleaks/dockle/trivy invocations
# (identical tool versions, flags, and severity thresholds) that .github/workflows/deploy.yml
# runs before any image reaches GHCR. Run manually before pushing, or via the pre-commit hook
# (scripts/hooks/pre-commit, installed with scripts/hooks/install.sh).
#
# Builds and scans all three images unconditionally, same as CI -- this is slow (the sandbox
# image alone takes 10+ minutes to build) by deliberate choice: catching what CI would catch,
# every time, beats catching it faster but incompletely.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd ../.. && pwd)"
source ./versions.sh # TRIVY_SEVERITY / TRIVY_TIMEOUT per image -- same values deploy.yml reads.

fail=0
run() { echo "== $* =="; "$@" || { echo "FAILED: $*"; fail=1; }; }

run ./gitleaks.sh "$REPO_ROOT"

declare -A CONTEXTS=(
  [frontend]="$REPO_ROOT"
  [agent]="$REPO_ROOT/agent"
  [sandbox]="$REPO_ROOT/agent/sandbox-image"
)

for name in frontend agent sandbox; do
  context="${CONTEXTS[$name]}"
  tag="ai-dev-workflow-$name:local-check"
  run ./hadolint.sh "$context/Dockerfile"
  run ./build-image.sh "$tag" "$context"
  run ./dockle.sh "$tag"
  run ./trivy-image.sh "$tag" "${TRIVY_SEVERITY[$name]}" "${TRIVY_TIMEOUT[$name]}"
done

exit $fail
