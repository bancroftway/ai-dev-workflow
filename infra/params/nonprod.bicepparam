// Deployment target: nonprod (home tenant). Deployed by .github/workflows/deploy.yml on every
// merge to dev. Image refs arrive via env vars (readEnvironmentVariable below) because az CLI
// does not allow mixing a .bicepparam file with extra -p overrides; the defaults keep a manual
// `az deployment group create` bootable without CI.
using '../main.bicep'

param namePrefix = 'aidw-nonprod'
param entraTenantId = '<home-tenant-id>' // TODO(bootstrap): set real tenant id
param entraAppId = '<sign-in-app-id>' // TODO(bootstrap): set real app registration id
param sqlAadAdminObjectId = '<aidw-sql-admins-object-id>' // TODO(bootstrap): Entra group object id
param sqlAadAdminLogin = 'aidw-sql-admins'
param sqlAadAdminPrincipalType = 'Group'

param frontendImage = readEnvironmentVariable('FRONTEND_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param agentImage = readEnvironmentVariable('AGENT_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param sandboxImageTag = readEnvironmentVariable('SANDBOX_IMAGE_TAG', 'latest')
