// Deployment target: nonprod (home tenant). Deployed by .github/workflows/deploy.yml on every
// merge to dev. Image refs arrive via env vars (readEnvironmentVariable below) because az CLI
// does not allow mixing a .bicepparam file with extra -p overrides; the defaults keep a manual
// `az deployment group create` bootable without CI.
using '../main.bicep'

param namePrefix = 'aidw-nonprod'
param entraTenantId = 'e83eaf75-cbe6-47a2-82bd-451f13dc8b54' // TODO(bootstrap): set real tenant id
param entraAppId = '33b4c021-6631-4d29-9906-0d675b44fa74' // TODO(bootstrap): set real app registration id
param sqlAadAdminObjectId = '911e6eaa-2415-4bad-87ca-c69d2b41e5c5' // TODO(bootstrap): Entra group object id
param sqlAadAdminLogin = 'aidw-sql-admins'
param sqlAadAdminPrincipalType = 'Group'

param frontendImage = readEnvironmentVariable('FRONTEND_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param agentImage = readEnvironmentVariable('AGENT_IMAGE', 'mcr.microsoft.com/k8se/quickstart:latest')
param sandboxImageTag = readEnvironmentVariable('SANDBOX_IMAGE_TAG', 'latest')
