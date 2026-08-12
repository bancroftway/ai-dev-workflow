#!/bin/sh
# Logs in the az CLI (used by AzureContainerInstanceProvider, SANDBOX_PROVIDER=azure) via this
# Container App's managed identity before the app starts serving -- there is no interactive
# `az login` available in a deployed container, and DefaultAzureCredential-equivalent ambient auth
# for the raw `az` CLI (as opposed to an Azure SDK client) means actually running `az login
# --identity` once at startup.
set -e

if [ -n "${AZURE_USE_MANAGED_IDENTITY:-}" ]; then
  echo "docker-entrypoint: logging in via managed identity..."
  az login --identity --output none
fi

exec uv run uvicorn main:app --host 0.0.0.0 --port "${PORT:-8123}"
