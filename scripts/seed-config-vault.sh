#!/usr/bin/env bash
# Seeds the target's <namePrefix>-config Key Vault: self-grants Key Vault Secrets Officer (the
# deploy SP's RBAC-Administrator role is already conditioned to allow exactly this, see
# infra/onboard-target.ps1), then creates every secret in docs/CONFIG.md's "required for a real
# deployment" section that isn't already set -- a real value if its CONFIG_<NAME> env var is
# non-empty, otherwise an obvious REPLACE-ME placeholder. Idempotent and additive only: never
# overwrites a value (real or placeholder) already in the vault. Shared by deploy.yml (every
# deploy) and seed-config-vault.yml (manual re-run after adding a new required secret).
#
# Usage: seed-config-vault.sh <target>
# Requires env: NAME_PREFIX, RESOURCE_GROUP, AZURE_CLIENT_ID, AZURE_TENANT_ID
# Optional env (a real value for each, else a placeholder is written): CONFIG_AUTH_SECRET,
# CONFIG_AUTH_GITHUB_ID, CONFIG_AUTH_GITHUB_SECRET, CONFIG_AIDW_AGENT_CLIENT_SECRET,
# CONFIG_AIDW_AGENT_SHARED_SECRET, CONFIG_ANTHROPIC_API_KEY, CONFIG_CLAUDE_CODE_OAUTH_TOKEN,
# CONFIG_GITHUB_TOKEN
set -euo pipefail

TARGET="$1"
VAULT="$NAME_PREFIX-config"

VAULT_ID=$(az keyvault show --name "$VAULT" --resource-group "$RESOURCE_GROUP" --query id -o tsv)
SP_ID=$(az ad sp show --id "$AZURE_CLIENT_ID" --query id -o tsv)
EXISTING=$(az role assignment list --assignee "$SP_ID" --role "Key Vault Secrets Officer" --scope "$VAULT_ID" --query '[0].id' -o tsv)
if [ -z "$EXISTING" ]; then
  az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets Officer" --scope "$VAULT_ID"
  echo "waiting for the grant to propagate..."
  for i in $(seq 1 12); do
    az keyvault secret list --vault-name "$VAULT" >/dev/null 2>&1 && break
    sleep 10
  done
fi

# Values that AREN'T credentials and already have a known-good answer sitting in this repo's own
# IaC -- no placeholder, no GitHub secret, just copied straight from the source of truth.
ENTRA_APP_ID=$(grep -oP "param entraAppId = '\K[^']+" "infra/params/$TARGET.bicepparam")
declare -A KNOWN=(
  [AZURE-TENANT-ID]="$AZURE_TENANT_ID"
  [AIDW-AGENT-APP-ID]="$ENTRA_APP_ID"
)
for name in "${!KNOWN[@]}"; do
  if az keyvault secret show --vault-name "$VAULT" --name "$name" >/dev/null 2>&1; then
    echo "skip $name (already set)"
    continue
  fi
  az keyvault secret set --vault-name "$VAULT" --name "$name" --value "${KNOWN[$name]}" >/dev/null
  echo "seeded $name"
done

# Everything below is a real credential -- never derivable from this repo, always from a
# CONFIG_<NAME> env var you set yourself (or left blank on purpose). Anything left unset gets an
# obvious placeholder instead of being silently skipped, so `az keyvault secret list --vault-name
# <name>-config` always shows you exactly what's real vs. still needs a value.
declare -A REQUIRED=(
  [AUTH-SECRET]="${CONFIG_AUTH_SECRET:-}"
  [AUTH-GITHUB-ID]="${CONFIG_AUTH_GITHUB_ID:-}"
  [AUTH-GITHUB-SECRET]="${CONFIG_AUTH_GITHUB_SECRET:-}"
  [AIDW-AGENT-CLIENT-SECRET]="${CONFIG_AIDW_AGENT_CLIENT_SECRET:-}"
  [AIDW-AGENT-SHARED-SECRET]="${CONFIG_AIDW_AGENT_SHARED_SECRET:-}"
  [ANTHROPIC-API-KEY]="${CONFIG_ANTHROPIC_API_KEY:-}"
  [CLAUDE-CODE-OAUTH-TOKEN]="${CONFIG_CLAUDE_CODE_OAUTH_TOKEN:-}"
  [GITHUB-TOKEN]="${CONFIG_GITHUB_TOKEN:-}"
)
for name in "${!REQUIRED[@]}"; do
  if az keyvault secret show --vault-name "$VAULT" --name "$name" >/dev/null 2>&1; then
    echo "skip $name (already set)"
    continue
  fi
  value="${REQUIRED[$name]}"
  if [ -z "$value" ]; then
    echo "seeding $name with a placeholder -- no CONFIG_${name//-/_} env var was configured"
    value="REPLACE-ME"
  else
    echo "seeding $name from its GitHub secret"
  fi
  az keyvault secret set --vault-name "$VAULT" --name "$name" --value "$value" >/dev/null
done
