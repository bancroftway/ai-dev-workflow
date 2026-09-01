// Deployment target: prod (home tenant). Deployed by .github/workflows/deploy.yml on every merge
// to main, alongside every customer-tenant target listed in .github/deploy-targets.json.
// A new customer target = copy this file to <slug>.bicepparam (slug <= 12 chars: the
// "<namePrefix>-config" Key Vault name caps at 24), fill in THEIR tenant/app ids, add the slug to
// deploy-targets.json, and create the matching GitHub Environment (see infra/README.md).
using '../main.bicep'

param namePrefix = 'aidw-prod'
param entraTenantId = '<home-tenant-id>' // TODO(bootstrap): set real tenant id
param entraAppId = '<sign-in-app-id>' // TODO(bootstrap): set real app registration id
param sqlAadAdminObjectId = '<aidw-sql-admins-object-id>' // TODO(bootstrap): Entra group object id
param sqlAadAdminLogin = 'aidw-sql-admins'
param sqlAadAdminPrincipalType = 'Group'

param frontendImage = readEnvironmentVariable('FRONTEND_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param agentImage = readEnvironmentVariable('AGENT_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param sandboxImageTag = readEnvironmentVariable('SANDBOX_IMAGE_TAG', 'latest')
