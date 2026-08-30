# Onboard a new deployment target (home env or customer tenant) for the build-once
# deploy-everywhere pipeline (.github/workflows/deploy.yml). Run once per target by a human with
# admin rights IN THAT TENANT; every az call is idempotent-ish (create-if-missing) so re-running
# after a partial failure is safe.
#
#   ./onboard-target.ps1 -Slug acme -TenantId <guid> -SubscriptionId <guid> `
#       -GitHubRepo owner/ai-dev-workflow [-Location eastus2]
#
# Slug rules: <=12 chars, lowercase alphanumeric -- "aidw-<slug>-config" (Key Vault) caps at 24.
# After this script: create the GitHub Environment "<slug>" with the printed variables, copy
# infra/params/prod.bicepparam to infra/params/<slug>.bicepparam with the printed ids, add the
# slug to .github/deploy-targets.json, merge -- the pipeline bootstraps the rest (first run goes
# red at smoke until the config vault is seeded; see infra/README.md).
param(
  [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{1,12}$')][string]$Slug,
  [Parameter(Mandatory)][string]$TenantId,
  [Parameter(Mandatory)][string]$SubscriptionId,
  [Parameter(Mandatory)][ValidatePattern('^[^/]+/[^/]+$')][string]$GitHubRepo,
  [string]$Location = 'eastus2'
)
$ErrorActionPreference = 'Stop'
$prefix = "aidw-$Slug"
$rg = "$prefix-rg"

az login --tenant $TenantId --only-show-errors | Out-Null
az account set --subscription $SubscriptionId

# --- 1. Deploy app registration + GitHub OIDC federated credential (no secret anywhere) ---
$deployAppName = "aidw-deploy-$Slug"
$deployAppId = az ad app list --display-name $deployAppName --query '[0].appId' -o tsv
if (-not $deployAppId) { $deployAppId = az ad app create --display-name $deployAppName --query appId -o tsv }
$deploySpId = az ad sp list --filter "appId eq '$deployAppId'" --query '[0].id' -o tsv
if (-not $deploySpId) { $deploySpId = az ad sp create --id $deployAppId --query id -o tsv }

$fedName = "aidw-deploy-$Slug-github"
$existingFed = az ad app federated-credential list --id $deployAppId --query "[?name=='$fedName'] | [0].name" -o tsv
if (-not $existingFed) {
  $fed = @{
    name = $fedName
    issuer = 'https://token.actions.githubusercontent.com'
    subject = "repo:${GitHubRepo}:environment:$Slug"
    audiences = @('api://AzureADTokenExchange')
  } | ConvertTo-Json -Compress
  az ad app federated-credential create --id $deployAppId --parameters $fed | Out-Null
}

# --- 2. Sign-in app registration: App Roles (Admin/Member), exposed API, assignment required ---
$signinAppName = "aidw-$Slug-signin"
$signinAppId = az ad app list --display-name $signinAppName --query '[0].appId' -o tsv
if (-not $signinAppId) { $signinAppId = az ad app create --display-name $signinAppName --sign-in-audience AzureADMyOrg --query appId -o tsv }
# App Roles manifest -- ids are arbitrary but must stay stable per app once assigned.
$roles = @(
  @{ allowedMemberTypes = @('User'); description = 'Manage org settings'; displayName = 'Admin'
     id = [guid]::NewGuid().Guid; isEnabled = $true; value = 'Admin' },
  @{ allowedMemberTypes = @('User'); description = 'Standard access'; displayName = 'Member'
     id = [guid]::NewGuid().Guid; isEnabled = $true; value = 'Member' }
)
$haveRoles = az ad app show --id $signinAppId --query 'length(appRoles)' -o tsv
if ($haveRoles -eq '0') {
  $rolesJson = ConvertTo-Json $roles -Depth 4 -Compress
  az ad app update --id $signinAppId --app-roles $rolesJson
}
# Expose api://<appId>/access_as_user (the OBO assertion scope src/auth.ts requests).
az ad app update --id $signinAppId --identifier-uris "api://$signinAppId" 2>$null
$signinSpId = az ad sp list --filter "appId eq '$signinAppId'" --query '[0].id' -o tsv
if (-not $signinSpId) { $signinSpId = az ad sp create --id $signinAppId --query id -o tsv }
# Assignment required: unassigned users cannot sign in at all -- Member baseline, enforced by
# Entra, zero app code. Assign users (free tier) or groups (needs Entra ID P1) to the roles in
# the portal's Enterprise Application blade.
az ad sp update --id $signinSpId --set appRoleAssignmentRequired=true

# --- 3. SQL AAD admin group (deploy SP is a member -> pipeline can grant/migrate/drain) ---
$groupName = 'aidw-sql-admins'
$groupId = az ad group list --display-name $groupName --query '[0].id' -o tsv
if (-not $groupId) { $groupId = az ad group create --display-name $groupName --mail-nickname $groupName --query id -o tsv }
az ad group member add --group $groupId --member-id $deploySpId 2>$null

# --- 4. Resource group + least-priv deploy grants ---
az group create --name $rg --location $Location | Out-Null
az role assignment create --assignee-object-id $deploySpId --assignee-principal-type ServicePrincipal `
  --role Contributor --scope "/subscriptions/$SubscriptionId/resourceGroups/$rg" 2>$null
# RBAC Administrator, conditioned to ONLY the 4 role definitions main.bicep assigns -- the
# template's roleAssignments need this; plain Contributor cannot create them, Owner is too much.
$cond = @(
  'b24988ac-6180-42a0-ab88-20f7382dd24c', # Contributor (agent -> RG, for ACI management)
  '7f951dda-4ed3-4680-a7ca-43fe172d538d', # AcrPull
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7', # Key Vault Secrets Officer
  '4633458b-17de-408a-b874-0445c86b69e6'  # Key Vault Secrets User
) | ForEach-Object { "{$_}" }
$condition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$($cond -join ', ' -replace '[{}]','')}))"
az role assignment create --assignee-object-id $deploySpId --assignee-principal-type ServicePrincipal `
  --role 'Role Based Access Control Administrator' --scope "/subscriptions/$SubscriptionId/resourceGroups/$rg" `
  --condition $condition --condition-version '2.0' 2>$null

# --- 5. What the human wires up next ---
Write-Host @"

Target '$Slug' onboarded. Now:

1. GitHub Environment '$Slug' (Settings > Environments) with variables:
     AZURE_CLIENT_ID       = $deployAppId
     AZURE_TENANT_ID       = $TenantId
     AZURE_SUBSCRIPTION_ID = $SubscriptionId
     RESOURCE_GROUP        = $rg
     ACR_NAME              = $($prefix -replace '-','')acr
     NAME_PREFIX           = $prefix
   Deployment branch policy: main only (dev for the nonprod target).

2. infra/params/$Slug.bicepparam (copy prod.bicepparam):
     namePrefix          = '$prefix'
     entraTenantId       = '$TenantId'
     entraAppId          = '$signinAppId'
     sqlAadAdminObjectId = '$groupId'

3. Add "$Slug" to the right branch list in .github/deploy-targets.json, PR, merge.
   FIRST deploy goes red at smoke until step 4 -- expected.

4. Seed the config vault $prefix-config (created by that first deploy) per infra/README.md,
   then restart both container apps and re-run the Deploy workflow.

5. Sign-in app '$signinAppName': add redirect URI
   https://<frontend-fqdn>/api/auth/callback/microsoft-entra-id (FQDN exists after deploy),
   create a client secret (goes in the config vault as AIDW-AGENT-CLIENT-SECRET; calendar its
   expiry), and assign users to the Admin/Member roles in the Enterprise Application blade.
"@
