# Onboard a new deployment target (home env or customer tenant) for the build-once
# deploy-everywhere pipeline (.github/workflows/deploy.yml). Run once per target by a human with
# admin rights IN THAT TENANT; every step is guarded by an existence pre-check, so re-running
# after a partial failure is safe.
#
#   ./onboard-target.ps1 -Slug nonprod -TenantId <guid> -SubscriptionId <guid> `
#       -GitHubRepo owner/ai-dev-workflow [-Location eastus2]
#
# Slug rules: <=12 chars, lowercase alphanumeric -- "aidw-<slug>-config" (Key Vault) caps at 24.
# Windows PowerShell 5.1 compatible: no stderr redirection on native az calls (5.1 wraps redirected
# native stderr into ErrorRecords, which throws under -ErrorAction Stop even on success), JSON
# always passed to az via @file (inline JSON loses its quotes to native arg parsing on Windows).
param(
  [Parameter(Mandatory)][ValidatePattern('^[a-z0-9]{1,12}$')][string]$Slug,
  [Parameter(Mandatory)][string]$TenantId,
  [Parameter(Mandatory)][string]$SubscriptionId,
  [Parameter(Mandatory)][ValidatePattern('^[^/]+/[^/]+$')][string]$GitHubRepo,
  [string]$Location = 'eastus2'
)
$ErrorActionPreference = 'Stop'
$env:AZURE_CORE_ONLY_SHOW_ERRORS = 'true'
$prefix = "aidw-$Slug"
$rg = "$prefix-rg"

function Invoke-Az {
  # az's exit code is the only reliable failure signal for a native call in PS 5.1 -- check it
  # after every call that must have succeeded, with the args echoed so the failing step is obvious.
  #
  # Quoting: az on Windows is az.cmd (a batch file), and PS 5.1 only auto-quotes args containing
  # WHITESPACE -- an arg like `length(appRoles)` or `key=value` goes through bare, and cmd's
  # parser dies on the parens/equals with "-o was unexpected at this time". Wrap any
  # metacharacter-bearing, whitespace-free arg in literal quotes ourselves (whitespace-bearing
  # args are left alone: PS already quotes those, and double-wrapping would break them).
  param([Parameter(Mandatory)][string[]]$AzArgs)
  $escaped = $AzArgs | ForEach-Object {
    if ($_ -notmatch '\s' -and $_ -match '[()|&^<>;,=%]') { '"' + $_ + '"' } else { $_ }
  }
  $out = az @escaped
  if ($LASTEXITCODE -ne 0) { throw "az $($AzArgs -join ' ') failed (exit $LASTEXITCODE)" }
  return $out
}

Write-Host "== Signing in to tenant $TenantId"
Invoke-Az @('login', '--tenant', $TenantId) | Out-Null
Invoke-Az @('account', 'set', '--subscription', $SubscriptionId) | Out-Null

# --- 1. Deploy app registration + GitHub OIDC federated credential (no secret anywhere) ---
Write-Host '== Deploy app registration + federated credential'
$deployAppName = "aidw-deploy-$Slug"
$deployAppId = Invoke-Az @('ad', 'app', 'list', '--display-name', $deployAppName, '--query', '[0].appId', '-o', 'tsv')
if (-not $deployAppId) { $deployAppId = Invoke-Az @('ad', 'app', 'create', '--display-name', $deployAppName, '--query', 'appId', '-o', 'tsv') }
$deploySpId = Invoke-Az @('ad', 'sp', 'list', '--filter', "appId eq '$deployAppId'", '--query', '[0].id', '-o', 'tsv')
if (-not $deploySpId) { $deploySpId = Invoke-Az @('ad', 'sp', 'create', '--id', $deployAppId, '--query', 'id', '-o', 'tsv') }

$fedName = "aidw-deploy-$Slug-github"
$existingFed = Invoke-Az @('ad', 'app', 'federated-credential', 'list', '--id', $deployAppId, '--query', "[?name=='$fedName'] | [0].name", '-o', 'tsv')
if (-not $existingFed) {
  $fedFile = Join-Path $env:TEMP "aidw-fed-$Slug.json"
  @{
    name = $fedName
    issuer = 'https://token.actions.githubusercontent.com'
    subject = "repo:${GitHubRepo}:environment:$Slug"
    audiences = @('api://AzureADTokenExchange')
  } | ConvertTo-Json | Out-File -Encoding ascii $fedFile
  Invoke-Az @('ad', 'app', 'federated-credential', 'create', '--id', $deployAppId, '--parameters', "@$fedFile") | Out-Null
  Remove-Item $fedFile
}

# --- 2. Sign-in app registration: App Roles (Admin/Member), exposed API, assignment required ---
Write-Host '== Sign-in app registration (App Roles, exposed API, assignment-required)'
$signinAppName = "aidw-$Slug-signin"
$signinAppId = Invoke-Az @('ad', 'app', 'list', '--display-name', $signinAppName, '--query', '[0].appId', '-o', 'tsv')
if (-not $signinAppId) { $signinAppId = Invoke-Az @('ad', 'app', 'create', '--display-name', $signinAppName, '--sign-in-audience', 'AzureADMyOrg', '--query', 'appId', '-o', 'tsv') }

# App Role ids are arbitrary but must stay stable per app once assigned -- only set on first run.
$haveRoles = Invoke-Az @('ad', 'app', 'show', '--id', $signinAppId, '--query', 'length(appRoles)', '-o', 'tsv')
if ($haveRoles -eq '0') {
  $rolesFile = Join-Path $env:TEMP "aidw-roles-$Slug.json"
  ConvertTo-Json @(
    @{ allowedMemberTypes = @('User'); description = 'Manage org settings'; displayName = 'Admin'
       id = [guid]::NewGuid().Guid; isEnabled = $true; value = 'Admin' },
    @{ allowedMemberTypes = @('User'); description = 'Standard access'; displayName = 'Member'
       id = [guid]::NewGuid().Guid; isEnabled = $true; value = 'Member' }
  ) -Depth 4 | Out-File -Encoding ascii $rolesFile
  Invoke-Az @('ad', 'app', 'update', '--id', $signinAppId, '--app-roles', "@$rolesFile") | Out-Null
  Remove-Item $rolesFile
}

# Expose api://<appId>/access_as_user (the OBO assertion scope src/auth.ts requests).
$haveUri = Invoke-Az @('ad', 'app', 'show', '--id', $signinAppId, '--query', 'length(identifierUris)', '-o', 'tsv')
if ($haveUri -eq '0') {
  Invoke-Az @('ad', 'app', 'update', '--id', $signinAppId, '--identifier-uris', "api://$signinAppId") | Out-Null
}
$signinSpId = Invoke-Az @('ad', 'sp', 'list', '--filter', "appId eq '$signinAppId'", '--query', '[0].id', '-o', 'tsv')
if (-not $signinSpId) { $signinSpId = Invoke-Az @('ad', 'sp', 'create', '--id', $signinAppId, '--query', 'id', '-o', 'tsv') }
# Assignment required: unassigned users cannot sign in at all -- Member baseline, enforced by
# Entra, zero app code. Assign users (free tier) or groups (needs Entra ID P1) to the roles in
# the portal's Enterprise Application blade.
Invoke-Az @('ad', 'sp', 'update', '--id', $signinSpId, '--set', 'appRoleAssignmentRequired=true') | Out-Null

# --- 3. SQL AAD admin group (deploy SP is a member -> pipeline can grant/migrate/drain) ---
Write-Host '== aidw-sql-admins group'
$groupName = 'aidw-sql-admins'
$groupId = Invoke-Az @('ad', 'group', 'list', '--display-name', $groupName, '--query', '[0].id', '-o', 'tsv')
if (-not $groupId) { $groupId = Invoke-Az @('ad', 'group', 'create', '--display-name', $groupName, '--mail-nickname', 'aidwsqladmins', '--query', 'id', '-o', 'tsv') }
$isMember = Invoke-Az @('ad', 'group', 'member', 'check', '--group', $groupId, '--member-id', $deploySpId, '--query', 'value', '-o', 'tsv')
if ($isMember -ne 'true') {
  Invoke-Az @('ad', 'group', 'member', 'add', '--group', $groupId, '--member-id', $deploySpId) | Out-Null
}

# --- 4. Resource group + least-priv deploy grants ---
Write-Host "== Resource group $rg + deploy SP role grants"
Invoke-Az @('group', 'create', '--name', $rg, '--location', $Location) | Out-Null
$scope = "/subscriptions/$SubscriptionId/resourceGroups/$rg"

function Grant-IfMissing {
  param([string]$Role, [string[]]$Extra = @())
  $existing = Invoke-Az @('role', 'assignment', 'list', '--assignee', $deploySpId, '--role', $Role, '--scope', $scope, '--query', '[0].id', '-o', 'tsv')
  if (-not $existing) {
    Invoke-Az (@('role', 'assignment', 'create', '--assignee-object-id', $deploySpId,
      '--assignee-principal-type', 'ServicePrincipal', '--role', $Role, '--scope', $scope) + $Extra) | Out-Null
  }
}

Grant-IfMissing -Role 'Contributor'
# RBAC Administrator, conditioned to ONLY the 4 role definitions main.bicep assigns -- the
# template's roleAssignments need this; plain Contributor cannot create them, Owner is too much.
$roleIds = @(
  'b24988ac-6180-42a0-ab88-20f7382dd24c', # Contributor (agent -> RG, for ACI management)
  '7f951dda-4ed3-4680-a7ca-43fe172d538d', # AcrPull
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7', # Key Vault Secrets Officer
  '4633458b-17de-408a-b874-0445c86b69e6'  # Key Vault Secrets User
) -join ', '
$condition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$roleIds}))"
Grant-IfMissing -Role 'Role Based Access Control Administrator' -Extra @('--condition', $condition, '--condition-version', '2.0')

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

2. infra/params/$Slug.bicepparam (copy prod.bicepparam if new):
     namePrefix          = '$prefix'
     entraTenantId       = '$TenantId'
     entraAppId          = '$signinAppId'
     sqlAadAdminObjectId = '$groupId'

3. Ensure "$Slug" is in the right branch list in .github/deploy-targets.json.
   FIRST deploy goes red at smoke until step 4 -- expected.

4. Seed the config vault $prefix-config (created by that first deploy) per infra/README.md,
   then restart both container apps and re-run the Deploy workflow.

5. Sign-in app '$signinAppName': add redirect URI
   https://<frontend-fqdn>/api/auth/callback/microsoft-entra-id (FQDN exists after deploy),
   create a client secret (goes in the config vault as AIDW-AGENT-CLIENT-SECRET; calendar its
   expiry), and assign users to the Admin/Member roles in the Enterprise Application blade.
"@
