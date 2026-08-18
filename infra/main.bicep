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

@description('Short, unique-ish name prefix for all resources (e.g. "aidevworkflow-dev").')
param namePrefix string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('GitHub OAuth App client id (frontend sign-in).')
@secure()
param authGithubId string

@description('GitHub OAuth App client secret (frontend sign-in).')
@secure()
param authGithubSecret string

@description('NextAuth session-signing secret (openssl rand -base64 32).')
@secure()
param authSecret string

@description('Shared GitHub Copilot PAT used by every Copilot SDK session regardless of signed-in user (see agent/README.md and the plan\'s D.3 scaling note on why this is shared, not per-user, today).')
@secure()
param copilotGithubToken string

@description('Entra tenant id (single-tenant deployment).')
param entraTenantId string

@description('The single Entra app registration\'s client id -- covers user sign-in, the exposed api://<id>/access_as_user scope, and the agent\'s on-behalf-of Key Vault exchange (see infra/README.md).')
param entraAppId string

@description('That app registration\'s client secret (used by both the frontend sign-in and the agent\'s OBO exchange).')
@secure()
param entraClientSecret string

@description('Shared secret between the frontend and the agent\'s session endpoints (x-aidw-secret) -- required now that those endpoints carry user Entra assertions.')
@secure()
param agentSharedSecret string

@description('Container image for the frontend, e.g. myacr.azurecr.io/ai-dev-workflow-frontend:sha. Left as a placeholder tag on first deploy; CI (deploy-frontend.yml) updates it on every push.')
param frontendImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Container image for the agent, e.g. myacr.azurecr.io/ai-dev-workflow-agent:sha. Left as a placeholder tag on first deploy; CI (deploy-agent.yml) updates it on every push.')
param agentImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

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
// ACI's own image reference always resolves through the ACR created below, regardless of the
// tag CI last pushed -- kept as its own var so provision() (agent/src/sandbox/azure_aci.py) and
// this template agree on the same "latest" convention without a second parameter to keep in sync.
var sandboxImage = '${acr.name}.azurecr.io/ai-dev-workflow-sandbox:latest'

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
        targetPort: 8123
        transport: 'http'
      }
      secrets: [
        { name: 'copilot-github-token', value: copilotGithubToken }
        { name: 'entra-client-secret', value: entraClientSecret }
        { name: 'agent-shared-secret', value: agentSharedSecret }
      ]
      registries: [
        { server: '${acr.name}.azurecr.io', identity: 'system' }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: agentImage
          env: [
            { name: 'GITHUB_TOKEN', secretRef: 'copilot-github-token' }
            { name: 'SANDBOX_PROVIDER', value: 'azure' }
            // On-behalf-of Key Vault exchange (agent/src/keyvault.py): the shared Entra app
            // registration's confidential-client credentials. The agent's MANAGED identity is
            // deliberately not involved -- it has no vault access; every vault read happens as
            // the signed-in user via their forwarded assertion.
            { name: 'AZURE_TENANT_ID', value: entraTenantId }
            { name: 'AIDW_AGENT_APP_ID', value: entraAppId }
            { name: 'AIDW_AGENT_CLIENT_SECRET', secretRef: 'entra-client-secret' }
            { name: 'AIDW_AGENT_SHARED_SECRET', secretRef: 'agent-shared-secret' }
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
        targetPort: 3000
        transport: 'http'
      }
      secrets: [
        { name: 'auth-github-id', value: authGithubId }
        { name: 'auth-github-secret', value: authGithubSecret }
        { name: 'auth-secret', value: authSecret }
        { name: 'entra-client-secret', value: entraClientSecret }
        { name: 'agent-shared-secret', value: agentSharedSecret }
      ]
      registries: [
        { server: '${acr.name}.azurecr.io', identity: 'system' }
      ]
    }
    template: {
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          env: [
            { name: 'AUTH_GITHUB_ID', secretRef: 'auth-github-id' }
            { name: 'AUTH_GITHUB_SECRET', secretRef: 'auth-github-secret' }
            { name: 'AUTH_SECRET', secretRef: 'auth-secret' }
            // Entra ID primary sign-in (src/auth.ts): the ONE shared app registration -- same
            // three values the agent gets. GitHub above is the LINKED account (repos/push), not
            // the sign-in.
            { name: 'AZURE_TENANT_ID', value: entraTenantId }
            { name: 'AIDW_AGENT_APP_ID', value: entraAppId }
            { name: 'AIDW_AGENT_CLIENT_SECRET', secretRef: 'entra-client-secret' }
            { name: 'AIDW_AGENT_SHARED_SECRET', secretRef: 'agent-shared-secret' }
            // Required behind Container Apps' ingress -- confirmed locally (Dockerfile smoke
            // test) that NextAuth otherwise rejects every request with UntrustedHost.
            { name: 'AUTH_TRUST_HOST', value: 'true' }
            // Pure env-var change per the plan's D table -- AGENT_URL is already externalized in
            // src/app/api/copilotkit/[[...slug]]/route.ts, defaulting to http://localhost:8123/.
            { name: 'AGENT_URL', value: 'https://${agentApp.properties.configuration.ingress.fqdn}' }
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

output acrLoginServer string = '${acr.name}.azurecr.io'
output frontendUrl string = 'https://${frontendApp.properties.configuration.ingress.fqdn}'
output agentInternalUrl string = 'https://${agentApp.properties.configuration.ingress.fqdn}'
