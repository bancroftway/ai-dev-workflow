#!/usr/bin/env bash
# Builds one of the repo's images. Usage: build-image.sh <tag> <build-context>
# The one place the actual `docker build` invocation lives -- CI and local runs call this instead
# of duplicating the command, so a built image is byte-for-byte the same regardless of caller.
set -euo pipefail

TAG="$1"
CONTEXT="$2"
docker build -t "$TAG" "$CONTEXT"
