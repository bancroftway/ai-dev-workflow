#!/usr/bin/env bash
# Lints a built image against CIS/best-practice checks. Usage: dockle.sh <image-ref>
# Docker-based; needs the host's docker socket to inspect an image already built by build-image.sh.
set -euo pipefail
export MSYS_NO_PATHCONV=1 # Git-Bash-on-Windows mangles container-side paths in -v otherwise.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/versions.sh"

IMAGE_REF="$1"
# --allow-filename settings.py: dockle's CIS-DI-0010 is a filename heuristic, not content-based --
# it flags the Azure SDK's own azure/core/settings.py module in every image that uses it. Actual
# secret CONTENT is trivy's job (scanners: secret), not dockle's.
# --ignore DKL-DI-0005: fires on an apt-get RUN baked into the upstream base image's own layer
# history (python:3.12-slim / node:22-slim / the dotnet SDK image), not anything in our
# Dockerfiles -- dockle inspects layer history, so nothing we add downstream can satisfy it.
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  "$DOCKLE_IMAGE" --exit-code 1 --exit-level warn \
  --accept-file settings.py \
  --ignore DKL-DI-0005 \
  "$IMAGE_REF"
