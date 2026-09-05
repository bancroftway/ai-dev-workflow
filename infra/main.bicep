// Core Azure infrastructure for ai-dev-workflow (architecture plan Section D).
//
// PARTIALLY VALIDATED: the sandbox-hosting pieces below (VNET/subnet delegation, ACI with a
// private IP, the user-assigned identity + AcrPull pull path) mirror a configuration actually
// exercised end-to-end against a real subscription during the Open Risk #1 investigation (plan
// Section C.3) -- real VNET, real delegated subnet, real ACI container groups (public and
// private IP), a real pushed sandbox image, and a real AzureContainerInstanceProvider.provision()
// call that connected via RuntimeConnection.for_uri and completed the JSON-RPC handshake. What's
// NOT validated is this exact main.bicep file verbatim (no Azure CLI or Bicep compiler was
// available to run `az bicep build`/`what-if` against the file itself) -- the underlying resource
// shapes are proven, the specific property wiring here has not been.
//
// Resolved, not excluded: the sandbox-hosting resource is Azure Container Instances, not any
// Container Apps primitive (dynamic sessions and plain Container Apps TCP-transport ingress were
// both tested and both failed with a reproducible connection timeout -- see the plan's Section
// C.3 for the full writeup).

@description('Short, unique-ish name prefix for all resources, "aidw-<target>" by convention (e.g. "aidw-nonprod", "aidw-prod", "aidw-<customer>"). Key Vault names are the tight constraint: "<namePrefix>-config" must stay within 24 chars, so the target slug caps at 12.')
param namePrefix string

@description('Azure region for all resources.')
param location string = resourceGroup().location

// The ~9 former @secure() params (auth secrets, provider tokens, entra client secret, shared
// secret) are GONE on purpose: both apps boot their secret config from configVault below
// (src/instrumentation.ts, agent/src/env_bootstrap.py) and the env-wins precedence meant any
// template-set secretRef silently overrode the vault (the transitional conflict flagged in
// docs/superpowers/specs/2026-08-30-keyvault-config-design.md:51). Seeding the vault is the
// runbook's job (infra/README.md); this template only wires non-secret structural env.

@description('Entra tenant id (single-tenant per deployment target).')
param entraTenantId string

@description('The single Entra app registration\'s client id -- covers user sign-in, the exposed api://<id>/access_as_user scope, and the agent\'s on-behalf-of Key Vault exchange (see infra/README.md).')
param entraAppId string

@description('Container image for the frontend, e.g. myacr.azurecr.io/ai-dev-workflow-frontend:sha. Placeholder default keeps a first, pre-CI deploy bootable; deploy.yml passes the real :sha via the FRONTEND_IMAGE env var read by infra/params/*.bicepparam.')
param frontendImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Container image for the agent, same convention as frontendImage (AGENT_IMAGE env var).')
param agentImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Tag of the sandbox image in this target\'s ACR that azure_aci.py provisions at runtime -- deploy.yml passes the git sha (SANDBOX_IMAGE_TAG env var), pinning sandboxes to the exact bytes CI scanned instead of a drifting :latest.')
param sandboxImageTag string = 'latest'

@description('Ingress targetPort for the agent app. 8123 always, except deploy.yml\'s bootstrap pass (AGENT_TARGET_PORT env var), which sets 80 to match frontendImage/agentImage\'s placeholder default -- Container Apps\' default TCP startup probe checks this exact port, and mcr.microsoft.com/k8se/quickstart listens on 80, not 8123. A probe that can never succeed times out exactly like the AcrPull deadlock this whole bootstrap pass exists to break, so the placeholder image and the port it actually listens on have to change together.')
param agentTargetPort int = 8123

@description('Ingress targetPort for the frontend app, same convention as agentTargetPort (FRONTEND_TARGET_PORT env var, 80 during bootstrap).')
param frontendTargetPort int = 3000

@description('Whether agentApp/frontendApp are wired to pull from the private ACR at all. false ONLY for deploy.yml\'s one-time "bootstrap container apps" pass (USE_ACR_REGISTRY env var), which exists to break an AcrPull chicken-and-egg: Container Apps tries to resolve every registry listed in `configuration.registries` whenever the resource is touched, regardless of which image the current revision actually requests -- so even switching to a public placeholder image (frontendImage/agentImage above) still hits the same 401 if the ACR is listed there before its AcrPull role assignment (below, itself keyed off this same not-yet-existing identity) has ever been granted. Omitting the ACR from `registries` entirely for that one bootstrap pass lets the container apps -- and their AcrPull grants -- finally succeed; the very next (unconditional, real-image) deploy defaults this back to true and pulls from ACR fine.')
param useAcrRegistry bool = true

@description('VNET address space for the sandbox subnet.')
param vnetAddressPrefix string = '10.10.0.0/16'

@description('Subnet address space, delegated to Microsoft.ContainerInstance/containerGroups.')
param aciSubnetAddressPrefix string = '10.10.1.0/24'

@description('Azure AD object id of the session database\'s AAD admin -- a person or group who can run the one-time CREATE USER grant for the agent\'s managed identity (see infra/README.md). Azure AD-only auth, no SQL password of any kind.')
param sqlAadAdminObjectId string

@description('Display name of the SQL AAD admin (shown in the Azure portal only).')
param sqlAadAdminLogin string

@description('Principal type of sqlAadAdminObjectId.')
@allowed(['User', 'Group'])
param sqlAadAdminPrincipalType string = 'User'

var acrName = replace('${namePrefix}acr', '-', '')
// ACI's own image reference always resolves through the ACR created below; the tag is pinned by
// the sandboxImageTag param so provision() (agent/src/sandbox/azure_aci.py) runs exactly what CI
// scanned and imported for this deploy.
var sandboxImage = '${acr.name}.azurecr.io/ai-dev-workflow-sandbox:${sandboxImageTag}'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // CI authenticates via `az acr login` under its own OIDC identity
  }
}

// Sandbox networking (plan Section C.3): validated live -- ACI-to-ACI raw TCP through a
// delegated subnet's private IPs completed cleanly (exitCode 0, no timeout), unlike Container
// Apps' internal TCP ingress, which timed out reproducibly against the identical protocol.
resource sandboxVnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${namePrefix}-vnet'
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressPrefix]
    }
    subnets: [
      {
        name: 'aci-subnet'
        properties: {
          addressPrefix: aciSubnetAddressPrefix
          delegations: [
            {
              name: 'aci-delegation'
              properties: {
                serviceName: 'Microsoft.ContainerInstance/containerGroups'
              }
            }
          ]
        }
      }
    ]
  }
}

// Identity the *sandbox* container groups run as (agent/src/sandbox/azure_aci.py's
// --assign-identity/--acr-identity) -- deliberately separate from the agent's own identity below,
// so a compromised sandbox (an untrusted repo's postCreateCommand, plan Section C.3 risk #4)
// only ever holds AcrPull, never the agent's own ACI-management permissions.
resource sandboxIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-sandbox-identity'
  location: location
}

resource sandboxAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  // Idempotency trap (applies to every roleAssignment in this file): guid() must seed on values
  // known at deployment start, so principalId can't be used (ARM rejects it). If an identity-holding
  // resource is ever deleted and recreated, its principalId changes under the same assignment name
  // and every later deploy fails with RoleAssignmentUpdateNotPermitted -- fix by deleting the stale
  // assignment (`az role assignment delete --ids ...`) and re-running the deploy (runbook note).
  name: guid(acr.id, sandboxIdentity.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: sandboxIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
  }
}

// Single source of truth for session metadata (session_store.py) -- replaces the old
// `.ai-dev-workflow/sessions.json` git-committed file. Azure AD-only auth (azureADOnlyAuthentication:
// true below): no SQL login/password of any kind to manage or rotate. Only the agent connects
// (it's the sole reader AND writer -- the frontend calls the agent's HTTP API, never SQL
// directly, so there's exactly one schema-aware client). Basic tier: one logical writer
// (agentApp is pinned minReplicas=maxReplicas=1), one small table, low volume.
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: '${namePrefix}-sql'
  location: location
  properties: {
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: sqlAadAdminPrincipalType
      login: sqlAadAdminLogin
      sid: sqlAadAdminObjectId
      azureADOnlyAuthentication: true
    }
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: 'Ai-Dev-Workflow'
  location: location
  sku: {
    name: 'Basic'
    tier: 'Basic'
  }
  properties: {
    maxSizeBytes: 2147483648 // 2 GB -- Basic's own cap, far more than one small metadata table needs
  }
}

// No VNet integration on the Container Apps environment today (real scope increase to add --
// flagged in Known gaps below, not bundled into this change), so Container Apps egress uses
// unpredictable public IPs. AAD-only auth is what's actually locked down here; this firewall rule
// just lets Azure-hosted callers reach the server at all.
resource sqlAllowAzureServices 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAllWindowsAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource agentApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-agent'
  location: location
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      // Internal-only: only the frontend app (same environment) needs to reach this (plan
      // Section D) -- never exposed publicly.
      ingress: {
        external: false
        targetPort: agentTargetPort
        transport: 'http'
      }
      registries: useAcrRegistry ? [
        { server: '${acr.name}.azurecr.io', identity: 'system' }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: agentImage
          env: [
            // Secrets (GITHUB_TOKEN, ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, AGENT_PROVIDER,
            // AIDW_AGENT_CLIENT_SECRET, AIDW_AGENT_SHARED_SECRET, ...) come from configVault at
            // boot (agent/src/env_bootstrap.py) -- deliberately NOT set here, because template-set
            // values win over the vault and would freeze rotation behind a redeploy.
            { name: 'SANDBOX_PROVIDER', value: 'azure' }
            // On-behalf-of Key Vault exchange (agent/src/keyvault.py): the shared Entra app
            // registration's confidential-client credentials. The agent's MANAGED identity is
            // deliberately not involved -- it has no vault access; every vault read happens as
            // the signed-in user via their forwarded assertion.
            { name: 'AZURE_TENANT_ID', value: entraTenantId }
            { name: 'AIDW_AGENT_APP_ID', value: entraAppId }
            // docker-entrypoint.sh runs `az login --identity` before starting uvicorn when this
            // is set -- the agent's own system-assigned identity (below), not the sandbox
            // identity, which only ever needs AcrPull, never ACI-management permissions.
            { name: 'AZURE_USE_MANAGED_IDENTITY', value: 'true' }
            { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
            { name: 'AZURE_ACI_SANDBOX_IMAGE', value: sandboxImage }
            { name: 'AZURE_ACI_VNET_NAME', value: sandboxVnet.name }
            { name: 'AZURE_ACI_SUBNET_NAME', value: 'aci-subnet' }
            { name: 'AZURE_ACI_IDENTITY', value: sandboxIdentity.id }
            // session_store.py's db.py picks the Azure AD token connection path whenever this is
            // set (vs. the local Trusted_Connection path for dev) -- see agent/src/db.py.
            { name: 'AZURE_SQL_SERVER', value: '${sqlServer.name}.database.windows.net' }
            { name: 'AZURE_SQL_DATABASE', value: sqlDatabase.name }
            // Org-wide coding-agent credential vault (agent/src/org_credential_vault.py) -- read
            // via this SAME standing managed identity (AZURE_USE_MANAGED_IDENTITY above), NOT the
            // OBO exchange the AIDW_AGENT_* vars above use for per-repo vaults. Deliberately a
            // different access pattern for a fleet-wide secret with no natural per-user owner --
            // see this file's orgVault/kvSecretsOfficerRoleAgent resources and Part 4 Ruling 1.
            { name: 'AZURE_ORG_VAULT_URI', value: orgVault.properties.vaultUri }
            // Boot-time configuration source (agent/src/env_bootstrap.py): every secret in
            // configVault becomes an env var unless already set above -- the explicit values
            // in this list win, so the vault only fills what this template does not set.
            { name: 'AZURE_CONFIG_VAULT_URI', value: configVault.properties.vaultUri }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      // Min 1, not 0: the agent holds in-memory intra-run LangGraph state (InMemorySaver);
      // scale-to-zero would drop any in-flight run on cold start (plan Section D).
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

resource frontendApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-frontend'
  location: location
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: frontendTargetPort
        transport: 'http'
      }
      registries: useAcrRegistry ? [
        { server: '${acr.name}.azurecr.io', identity: 'system' }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          env: [
            // Secrets (AUTH_GITHUB_ID/SECRET, AUTH_SECRET, AIDW_AGENT_CLIENT_SECRET,
            // AIDW_AGENT_SHARED_SECRET, ...) come from configVault at boot
            // (src/instrumentation.ts) -- deliberately NOT set here; template-set values would
            // win over the vault (env-wins precedence).
            // Entra ID primary sign-in (src/auth.ts): the ONE shared app registration -- same
            // values the agent gets. GitHub is the LINKED account (repos/push), not the sign-in.
            { name: 'AZURE_TENANT_ID', value: entraTenantId }
            { name: 'AIDW_AGENT_APP_ID', value: entraAppId }
            // Required behind Container Apps' ingress -- confirmed locally (Dockerfile smoke
            // test) that NextAuth otherwise rejects every request with UntrustedHost.
            { name: 'AUTH_TRUST_HOST', value: 'true' }
            // Pure env-var change per the plan's D table -- AGENT_URL is already externalized in
            // src/app/api/copilotkit/[[...slug]]/route.ts, defaulting to http://localhost:8123/.
            { name: 'AGENT_URL', value: 'https://${agentApp.properties.configuration.ingress.fqdn}' }
            // Boot-time configuration source (src/instrumentation.ts) -- same precedence rule as
            // the agent: values listed here win, the vault fills the rest.
            { name: 'AZURE_CONFIG_VAULT_URI', value: configVault.properties.vaultUri }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

// Lets the agent's own identity create/delete ACI container groups (agent/src/sandbox/azure_aci.py)
// via `az container create/delete`. Scoped to the whole resource group, not just ACI resources --
// there is no built-in role narrower than Contributor for container-instance management alone.
// Flagged, not fixed: a real least-privilege follow-up (a custom role restricted to
// Microsoft.ContainerInstance/* actions) once this moves past "small internal tool" (plan
// Decision 4) -- Contributor is broader than this identity strictly needs today.
resource aciManagementRoleAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, agentApp.id, 'Contributor')
  properties: {
    principalId: agentApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c') // Contributor
  }
}

resource acrPullRoleAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, agentApp.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: agentApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
  }
}

resource acrPullRoleFrontend 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, frontendApp.id, 'AcrPull')
  scope: acr
  properties: {
    principalId: frontendApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
  }
}

// Org-wide coding-agent credential (Anthropic API key or Copilot PAT), Part 4 org settings --
// dedicated, minimal vault for exactly one secret. Deliberately separate from
// agent/src/keyvault.py's per-repo vaults (customer-owned, reached only via OBO -- the agent holds
// no standing access there, by design). That design doesn't fit here: this secret is fleet-wide
// with no natural per-user owner to exchange an OBO assertion on behalf of (plan Part 4 Ruling 1),
// so the agent's OWN managed identity gets standing access below, scoped to this one vault only.
resource orgVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  // Key Vault names are globally unique across all of Azure AND capped at 24 chars -- tighter
  // than every other resource name in this file (ACR tolerates 50 chars, just no hyphens). Relies
  // on the same "namePrefix is already unique-ish" assumption acr's own name does, just with less
  // headroom -- a long namePrefix fails this specific resource at deploy time before anything else.
  name: '${namePrefix}-vault'
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    // RBAC, not the legacy vault access-policy model -- required for the role assignment below to
    // have any effect at all; under the access-policy model it would silently grant nothing.
    enableRbacAuthorization: true
  }
}

// Standing access for the org-wide credential -- Ruling 1's deliberate divergence from the OBO
// pattern everywhere else in this codebase: the agent's OWN system-assigned identity, not a
// per-user assertion, because a fleet-wide secret has no natural per-user owner to delegate
// through (contrast agent/src/keyvault.py's module docstring, which is the pattern this is NOT).
//
// Role: built-in "Key Vault Secrets Officer" (dataActions: Microsoft.KeyVault/vaults/secrets/*,
// i.e. full CRUD on secrets specifically, NOT vault administration or permission management).
// The plan's Ruling 1 and this task's own brief both say "Key Vault Secrets User" -- deliberately
// NOT followed literally here, because that role's dataActions are only getSecret + readMetadata
// (verified against Microsoft's own built-in-roles reference just before writing this). It cannot
// set a secret. org_credential_vault.py's set_org_credential runs under this exact identity
// (called from sessions_api.py inside this same container, per Part 4 Task 6) and needs the
// setSecret data action -- Secrets User would 403 the very first time an admin saves an org
// credential. Secrets Officer is the minimal built-in role that actually covers both
// get_org_credential and set_org_credential, still without widening into vault administration or
// permission management. Flagged in the Task 2 report as a deliberate spec deviation, not a
// silent one.
//
// Scope: this ONE vault's resource ID only, never the resource group -- verify the `scope` in the
// generated ARM JSON after `az bicep build`, don't trust the source read alone (Ruling 1's own
// stated cost-if-wrong: an over-scoped grant here would be a real security regression).
resource kvSecretsOfficerRoleAgent 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(orgVault.id, agentApp.id, 'KeyVaultSecretsOfficer')
  scope: orgVault
  properties: {
    principalId: agentApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7') // Key Vault Secrets Officer
  }
}

// Service configuration vault (docs/superpowers/specs/2026-08-30-keyvault-config-design.md):
// every secret in it becomes an env var in BOTH apps at boot (src/instrumentation.ts,
// agent/src/env_bootstrap.py), replacing the Container Apps secrets above over time. Separate
// from orgVault on purpose: that one holds runtime-WRITTEN secrets (org-provider-credential,
// GitHub links) which must never be injected as env vars. Read-only for both identities --
// seeding is out-of-band via `az keyvault secret set` (infra/README.md), so "Key Vault Secrets
// User" (getSecret + list) is exactly enough here.
resource configVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-config' // same 24-char global-uniqueness caveat as orgVault
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
  }
}

resource kvSecretsUserRoleAgentConfig 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(configVault.id, agentApp.id, 'KeyVaultSecretsUser')
  scope: configVault
  properties: {
    principalId: agentApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6') // Key Vault Secrets User
  }
}

resource kvSecretsUserRoleFrontendConfig 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(configVault.id, frontendApp.id, 'KeyVaultSecretsUser')
  scope: configVault
  properties: {
    principalId: frontendApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6') // Key Vault Secrets User
  }
}

output acrLoginServer string = '${acr.name}.azurecr.io'
output frontendUrl string = 'https://${frontendApp.properties.configuration.ingress.fqdn}'
output agentInternalUrl string = 'https://${agentApp.properties.configuration.ingress.fqdn}'
