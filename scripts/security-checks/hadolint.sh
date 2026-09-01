#!/usr/bin/env bash
# Lints a Dockerfile. Usage: hadolint.sh <path/to/Dockerfile>
# Docker-based (not a host install) so this runs identically here and in CI -- same image, same
# flags, sourced from versions.sh in both places.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/versions.sh"

DOCKERFILE="$1"
docker run --rm -i "$HADOLINT_IMAGE" hadolint --failure-threshold warning - < "$DOCKERFILE"
