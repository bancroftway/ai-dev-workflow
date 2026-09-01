# Config from Azure Key Vault (web app + agent)

Date: 2026-08-30. Status: approved.

## Problem

Both processes read their configuration from the process environment, populated locally by the
repo-root `.env` and in production by Container Apps secrets/env set in `infra/main.bicep`
(Decision 4 in `infra/README.md`: "no Key Vault for the service's own secrets"). Approaching
production, secrets must live in Key Vault, and a developer machine should need exactly one
configuration value: where that vault is.

## Decision

Each process loads every secret from one dedicated **config vault** at boot and injects it into
its own environment before any module that reads configuration is imported. One mechanism, same
in local dev and production; `.env` shrinks to a single line.

- **Vault:** one config vault per environment, separate from `AZURE_ORG_VAULT_URI` (that vault
  holds runtime-written secrets such as `org-provider-credential`, which must not become env
  vars). Dev: `https://aidw-kv-dev.vault.azure.net/`. Prod: `${namePrefix}-config` (bicep).
- **Bootstrap variable:** `AZURE_CONFIG_VAULT_URI`. Unset: loader is skipped (env/.env only,
  today's behaviour). Set but unreachable: boot fails with Azure's own error text.
- **Naming:** secret `AUTH-SECRET` -> env `AUTH_SECRET` (upper-case, `-` -> `_`). Every secret
  in the vault is injected; no allowlist, no tags.
- **Precedence:** an already-set process variable wins; the vault fills the rest. Platform-set
  values (`PORT`, `AGENT_URL`, `AZURE_USE_MANAGED_IDENTITY`, `SANDBOX_PROVIDER`) and shell
  overrides (`AIDW_E2E_MODE=1`) stay authoritative.
- **Identity:** `DefaultAzureCredential` in both runtimes -- `az login` locally, the Container
  App's system-assigned managed identity in production (`Key Vault Secrets User` on the config
  vault only).
- **Refresh:** boot-time only. Rotating a secret means restarting the process.

## Components

| Unit | Does | Depends on |
|---|---|---|
| `src/instrumentation.ts` | Next.js `register()` (Node runtime only; Next 16 awaits it before serving requests, and `proxy.ts` runs on Node too). Lists + reads secrets, `process.env[name] ??= value`, logs the count. | `@azure/identity`, `@azure/keyvault-secrets` (new deps) |
| `agent/src/env_bootstrap.py` | `bootstrap_env()` = `load_dotenv(find_dotenv())` then vault load with the sync `azure.identity` / `azure.keyvault.secrets` clients (already deps). `_demo()` self-checks the name mapping and precedence offline. | replaces the `load_dotenv` calls in `agent/main.py`, `agent/run_headless.py`, `agent/test_requirements_delta_e2e.py` |
| `infra/main.bicep` | Additive: `configVault` resource, `Key Vault Secrets User` role for both apps' identities, `AZURE_CONFIG_VAULT_URI` env on both apps. Existing param-driven `secretRef` env stays (env wins, so nothing changes until the prod vault is seeded). | -- |
| `.env`, `.env.example`, `README.md`, `infra/README.md` | `.env` = the vault URI line. Docs describe naming, precedence, the `az keyvault secret set` seeding loop, and the role grant. | -- |

## Seeding

One-time per environment, out of band (same posture as the SQL grant in `infra/README.md`):
for each `NAME=value` line, `az keyvault secret set --vault-name <vault> --name <NAME with _ -> ->
--value <value>`. Dev vault seeded from the current `.env` as part of this change.

## Out of scope

Removing the secret parameters from `main.bicep` (follow-up once the prod vault is populated);
per-request refresh/caching; the sandbox containers (they get user-app secrets via
`agent/src/keyvault.py`'s OBO path, unchanged).

## Verification

1. `.env` reduced to one line, `.\dev.ps1`: both processes log `config vault: N values loaded`,
   Microsoft sign-in works, Org Settings GET returns JSON.
2. `cd agent && uv run python -m src.env_bootstrap` passes its self-check.
3. `npm run build` passes; `az bicep build --file infra/main.bicep` passes.
4. `AZURE_CONFIG_VAULT_URI` pointed at a non-existent vault: both processes refuse to start with
   the Azure error in the log.
