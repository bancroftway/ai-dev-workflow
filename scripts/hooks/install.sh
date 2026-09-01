#!/usr/bin/env bash
# One-time setup: points git at this repo's versioned hooks directory instead of .git/hooks
# (which isn't checked in). Run once per clone: scripts/hooks/install.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
git config core.hooksPath scripts/hooks
echo "pre-commit hook installed (git config core.hooksPath scripts/hooks)"
