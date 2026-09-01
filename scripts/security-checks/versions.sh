# Single source of truth for every security tool's pinned version. Sourced by every script in
# this directory and by .github/workflows/ci.yml + deploy.yml -- bump a tool here, everywhere
# (local and CI) picks it up identically. Never reference these images/tags anywhere else.
HADOLINT_IMAGE="hadolint/hadolint:v2.15.1"
GITLEAKS_IMAGE="zricethezav/gitleaks:v8.24.3"
DOCKLE_IMAGE="goodwithtech/dockle:v0.4.15"
TRIVY_IMAGE="aquasec/trivy:0.70.0"
