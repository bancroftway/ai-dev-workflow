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

**Partially validated.** The underlying resource shapes above are proven; this exact
`main.bicep` file has not been run through `az bicep build`/`what-if` verbatim (no Bicep
compiler was available in the environment it was authored in). Before applying it:

```bash
az bicep build --file main.bicep         # syntax check
az deployment group validate \
  --resource-group <rg> \
  --template-file main.bicep \
  --parameters namePrefix=<prefix> authGithubId=<id> authGithubSecret=<secret> authSecret=<secret> \
               copilotGithubToken=<token> sqlAadAdminObjectId=<your-aad-object-id> sqlAadAdminLogin=<your-upn-or-group-name> \
               entraTenantId=<tenant-id> entraAppId=<app-client-id> entraClientSecret=<secret> \
               agentSharedSecret=<random>
az deployment group what-if \
  --resource-group <rg> \
  --template-file main.bicep \
  --parameters @params.json   # review before creating params.json with real secrets, don't commit it
```

`sqlAadAdminObjectId`/`sqlAadAdminLogin` (and `sqlAadAdminPrincipalType`, default `User`) name
whoever should be the SQL Server's Azure AD admin — needed once, right after deploy, to run the
one-time grant below. Get your own object id with `az ad signed-in-user show --query id -o tsv`.

## First deploy

The container apps need *some* image to deploy with before CI has ever pushed one — that's what
`frontendImage`/`agentImage`'s placeholder default is for. The sandbox image has no such
placeholder: `AzureContainerInstanceProvider` reads it from `AZURE_ACI_SANDBOX_IMAGE`
(`main.bicep` points this at `<acr>.azurecr.io/ai-dev-workflow-sandbox:latest`), so
`build-sandbox-image.yml` needs to have pushed at least once before the first real onboarding
session can provision a sandbox. After the first `az deployment group create`, push real images
(manually or by running the GitHub Actions workflows once `ACR_NAME`/`RESOURCE_GROUP`/
`CONTAINER_APP_NAME_*` repo variables and the OIDC login secrets are configured — see the
comments at the top of `.github/workflows/deploy-*.yml`), then subsequent pushes to `main` keep
everything current automatically.

## One-time Entra app registration (sign-in + on-behalf-of Key Vault)

ONE app registration, created once per tenant, covers all three roles: user sign-in, the exposed
API scope, and the OBO confidential client (same-app OBO is a documented, supported pattern; the
split-registration variant was deliberately collapsed — fewest moving parts for a single-tenant
internal tool). The design: users sign in with Entra ID; the access token (audience = this same
app) is forwarded at provision time as an OBO assertion; the agent (`agent/src/keyvault.py`)
exchanges it on-behalf-of the user for a Key Vault token. **The service has no standing vault
access** — Azure evaluates the *user's* RBAC on every vault read.

Portal (portal.azure.com → Microsoft Entra ID → App registrations → New registration):

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
4. **Certificates & secrets** → New client secret → copy the Value →
   `entraClientSecret` / `AIDW_AGENT_CLIENT_SECRET`.

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

## One-time SQL grant (run once per environment, after the first deploy)

Bicep can't run T-SQL, so the agent's managed identity needs a one-time, out-of-band grant on the
new `Ai-Dev-Workflow` database before `session_store.py` can connect. As the AAD admin configured
above (`sqlcmd` with AAD auth, or the Azure portal's Query Editor):

```sql
CREATE USER [<namePrefix>-agent] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<namePrefix>-agent];
ALTER ROLE db_datawriter ADD MEMBER [<namePrefix>-agent];
```

`<namePrefix>-agent` is the Container App's own name (its system-assigned identity's display
name matches it) -- the same `namePrefix` parameter used everywhere else in this template. Only
`agentApp` needs this; the frontend never touches SQL directly (it calls the agent's `/sessions`
HTTP API), so there's exactly one identity to grant and exactly one schema-aware client.

Then run migrations once against the new database (from a machine with `az` AAD auth and network
access to the server, or via Cloud Shell): `cd agent && uv run python -m src.db_migrate` with
`AZURE_SQL_SERVER`/`AZURE_SQL_DATABASE` set to match the deployed values (`main.bicep` already
sets these as the agent Container App's own env, but the migration runner needs them in
whichever shell invokes it too).

## Known gaps

- No Key Vault *for the service's own secrets* — those are Container Apps' built-in secrets, per
  Decision 4 (small internal tool, sufficient at this scale). User-app secrets are different:
  they come from per user-repo Key Vaults read on-behalf-of the user (section above).
- No custom domain/TLS config beyond the platform-managed `*.azurecontainerapps.io` certificate.
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
