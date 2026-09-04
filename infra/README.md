# Infrastructure (architecture plan Section D)

`main.bicep` provisions: Log Analytics, Azure Container Registry, a Container Apps environment,
the two persistent services (frontend, agent), an Azure SQL Database (session metadata --
`agent/src/session_store.py`, replacing the old `.ai-dev-workflow/sessions.json` git-committed
file), and the sandbox networking/identity pieces (a VNET with a subnet delegated to
`Microsoft.ContainerInstance/containerGroups`, and a user-assigned identity for sandbox
containers with AcrPull already granted). It does **not** provision the per-session sandbox
containers themselves — those are created and destroyed on demand by
`agent/src/sandbox/azure_aci.py` (`AzureContainerInstanceProvider`) at runtime, one ACI container
group per session, not by this template.

The sandbox-hosting *decision* (Azure Container Instances, not any Container Apps primitive) was
resolved by testing live against a real subscription — see the comment at the top of
`main.bicep` and the plan's Section C.3 for the full writeup. The VNET/subnet/identity shapes
here mirror what was actually exercised end-to-end in that session (real VNET, real delegated
subnet, real ACI container groups with both public and private IPs, a real pushed sandbox image,
a real `AzureContainerInstanceProvider.provision()` call that connected and completed the
JSON-RPC handshake).

The template is CI-validated on every PR (`az bicep build` + `build-params` in
`.github/workflows/ci.yml`), and **no secrets are parameters anymore**: both apps boot their
secret config from the `<namePrefix>-config` Key Vault (see "Config vault seeding" below), so a
deployment needs only the ids in `infra/params/<target>.bicepparam`.

## Deployment targets and the pipeline

Deploys are fully pipeline-driven (`.github/workflows/deploy.yml`, see the root README's
"Deployment pipeline" section): merge to `dev` deploys every target listed under `dev` in
`.github/deploy-targets.json` (home `nonprod`), merge to `main` deploys every target under
`main` (home `prod` plus every customer tenant, simultaneously). A **target** is:

- a GitHub Environment named `<target>` carrying `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_SUBSCRIPTION_ID`, `RESOURCE_GROUP`, `ACR_NAME`, `NAME_PREFIX` as environment variables
  (no secrets: OIDC federation replaces credentials), and
- `infra/params/<target>.bicepparam` with that tenant's ids (`namePrefix = aidw-<target>`).

### Onboarding a new target (env or customer tenant)

Run `infra/onboard-target.ps1` as an admin in the target tenant — it creates the deploy app
registration + federated credential, the sign-in app registration with the `Admin`/`Member` App
Roles and assignment-required, the `aidw-sql-admins` group (deploy SP as member, so the pipeline
can run the SQL grant/migrations/drain), the resource group, and the least-privilege deploy
grants (Contributor + conditioned RBAC Administrator). It prints the GitHub Environment values
and the bicepparam skeleton. Then follow its printed steps 1-5.

**The first Deploy run for a new target is EXPECTED to fail at the smoke step**: the deploy job
itself seeds `<namePrefix>-config` with `REPLACE-ME` placeholders (below), but both apps still
crash-loop until a human pastes real values over them. Fill in the vault, restart both container
apps (`az containerapp revision restart`), re-run the Deploy workflow — every step is idempotent.

### Config vault seeding

`deploy.yml` creates `<namePrefix>-config`; the pipeline never holds app secrets, so something
else has to write into it: one secret per env var, `_` → `-` (`AUTH_SECRET` → `AUTH-SECRET`); the
name inventory is docs/CONFIG.md's "Secrets / identity (required for a real deployment)" section.

**Automatic, every deploy (creates only what's missing, never overwrites a real value):**
`deploy.yml`'s "seed config vault (placeholders)" step runs `scripts/seed-config-vault.sh` right
after the bicep step, on every push. It self-grants `Key Vault Secrets Officer` on the vault (the
deploy SP's RBAC-Administrator role is already conditioned to allow exactly this, see
onboard-target.ps1), then seeds two kinds of value:

- Non-secret, already known from this repo's own IaC -- `AZURE_TENANT_ID` (from the environment's
  own `vars.AZURE_TENANT_ID`) and `AIDW_AGENT_APP_ID` (parsed straight out of
  `infra/params/<target>.bicepparam`'s `entraAppId`). No GitHub secret needed for either.
- Everything else, a real credential: uses that environment's `CONFIG_<NAME>` GitHub secret if
  you've set one (e.g. `CONFIG_AUTH_SECRET` for `AUTH_SECRET`), otherwise writes an obvious
  `REPLACE-ME` placeholder so `az keyvault secret list` shows you exactly what still needs a real
  value.

**Manual re-run (adding a newly-required secret to an already-deployed target without waiting for
the next push):** the same script via the `Seed Config Vault` workflow (`workflow_dispatch`,
target = the GitHub Environment name).

**Fully manual (copying from an existing environment, or fixing up one secret):**
`az keyvault secret show` → `set` loop, same `_`→`-` naming. Grant yourself
`Key Vault Secrets Officer` on the vault first.

Either way: client secrets expire (≤24 months) — calendar the rotation.

### Idempotency notes

- `az deployment group create` (incremental mode) is safe to re-run; a no-op deploy takes ~1-2
  minutes. Resources *removed* from the template are NOT deleted — clean up manually.
- Key Vault soft-delete: a deleted `<prefix>-vault`/`-config` blocks recreation under the same
  name for 90 days (`az keyvault recover`, or purge). Purge protection is deliberately off for
  now (documented prod-hardening option).
- Role assignments seed `guid()` on resource ids (ARM can't use principalIds in names). If an
  identity-holding resource is deleted and recreated, delete the stale assignment
  (`az role assignment delete`) before the next deploy or it fails with
  `RoleAssignmentUpdateNotPermitted`.
- Rollback = re-run the Deploy workflow from the last good commit (images are `:sha`-pinned,
  the bicep deployment records them).

## One-time Entra app registration (sign-in + on-behalf-of Key Vault)

ONE app registration, created once per tenant, covers all three roles: user sign-in, the exposed
API scope, and the OBO confidential client (same-app OBO is a documented, supported pattern; the
split-registration variant was deliberately collapsed — fewest moving parts for a single-tenant
internal tool). The design: users sign in with Entra ID; the access token (audience = this same
app) is forwarded at provision time as an OBO assertion; the agent (`agent/src/keyvault.py`)
exchanges it on-behalf-of the user for a Key Vault token. **The service has no standing vault
access** — Azure evaluates the *user's* RBAC on every vault read.

`infra/onboard-target.ps1` scripts all of this (plus the App Roles below); the portal steps are
kept for reference/repair:

1. Name e.g. `<prefix>-app` · **Accounts in this organizational directory only** · Redirect URI:
   platform **Web**, `http://localhost:3000/api/auth/callback/microsoft-entra-id` (add
   `https://<frontend-fqdn>/api/auth/callback/microsoft-entra-id` for prod) → Register.
   Overview page: client id → `entraAppId` / `AIDW_AGENT_APP_ID`; tenant id → `entraTenantId` /
   `AZURE_TENANT_ID`.
2. **Expose an API** → set the Application ID URI (default `api://<client-id>`) → **Add a
   scope** named `access_as_user`, consent "Admins and users", enabled.
3. **API permissions** → Add a permission → **My APIs** → this same app → Delegated →
   `access_as_user` → Add → **Grant admin consent** (the app requests its own scope at sign-in;
   without consent every login prompts or fails with AADSTS65001).
4. **Certificates & secrets** → New client secret → copy the Value → the config vault's
   `AIDW-AGENT-CLIENT-SECRET` (no longer a bicep parameter).
5. **App roles** → two roles, both "Users/Groups": `Admin` (value `Admin`, gates Org settings)
   and `Member` (value `Member`, standard access). Then in the **Enterprise application** blade:
   Properties → **Assignment required = Yes** (unassigned users can't sign in at all), and
   Users and groups → assign people to the roles. Group-to-role assignment needs Entra ID P1;
   direct user assignment works on the free tier. Role changes take effect at the user's next
   sign-in (the app never re-reads claims mid-session).

Per-user vault access (each vault's owner, once per user): grant `Key Vault Secrets User` —

```bash
az role assignment create --role "Key Vault Secrets User" \
  --assignee <user-upn> --scope <vault-resource-id>
```

Users point a repo at their vault via the app's per-repo settings page (gear icon on the repo
list); the save test-reads the vault as them, so a bad grant fails loudly at configure time.
Notes: apps that read Key Vault *themselves* at startup work by storing their own service
principal's `AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET` as vault secrets (the env
injection makes `DefaultAzureCredential`'s environment path work inside the sandbox). Tenant
Conditional Access policies can block the OBO exchange (AADSTS530xx) — the error detail is
surfaced verbatim in the settings page and provision failures.

## SQL grant + migrations (automated)

The pipeline runs both on every deploy, idempotently, as the deploy SP (a member of the
`aidw-sql-admins` group, which is the SQL Server's AAD admin): the `IF NOT EXISTS`-guarded
`CREATE USER [<namePrefix>-agent] FROM EXTERNAL PROVIDER` + `db_datareader`/`db_datawriter`
grants, then `uv run python -m src.db_migrate`. Only `agentApp` gets a grant; the frontend never
touches SQL directly (it calls the agent's `/sessions` HTTP API). Manual fallback (portal Query
Editor as any `aidw-sql-admins` member) is the same SQL — see `deploy.yml`'s grant step.

## Known gaps

- New required config keys crash-loop every tenant whose vault lacks them: ship new keys with a
  safe in-code default, or seed ALL target vaults before the introducing merge lands.
- No custom domain/TLS config beyond the platform-managed `*.azurecontainerapps.io` certificate
  — a likely customer-tenant ask, unaddressed today.
- The agent's own identity is granted resource-group-scoped **Contributor** to manage ACI
  container groups — broader than strictly needed (no built-in role is narrower for
  container-instance management alone). A custom least-privilege role is the follow-up once this
  moves past "small internal tool" scale.
- `exec_in_sandbox` in `azure_aci.py` (needed by Section B's persistence layer against an Azure
  sandbox) uses the same pattern as the local Docker provider but has not been verified to
  survive `az container exec`'s command-line handling the way `docker exec` does — ACI's
  container-*create*-time `--command-line` was empirically found to naively whitespace-split
  rather than invoke a shell, which is exactly the kind of thing that silently breaks shell
  operators (`&&`, `|`, `>`) if `exec` behaves the same way. Verify before relying on it.
- The SQL Server has no VNet integration/private endpoint — the Container Apps environment isn't
  VNet-injected today, so a tight firewall rule isn't practical without adding that (real scope
  increase). Azure AD-only auth is the actual lock (no SQL password exists to leak); the
  `AllowAllWindowsAzureIps` firewall rule just admits Azure-hosted callers at the network layer.
