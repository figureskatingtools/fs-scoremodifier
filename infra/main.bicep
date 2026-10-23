targetScope = 'subscription'

// Backend-only infrastructure for the Score Modifier tool.
//
// The frontend, Easy Auth, DNS and custom-domain binding used to live here as a
// dedicated B1 Web App on scoremodifier.figureskatingtools.com. They now live in
// the figureskatingtools-site repo, which hosts every tool under one domain
// (figureskatingtools.com/scoremodifier/) behind a single router Web App. That
// router proxies /scoremodifier/api/* here, injecting X-Proxy-Secret and
// X-Forwarded-User-Email (see PROXY-CONTRACT.md). This deployment therefore only
// manages the storage account, the Function App and its RBAC.

param location string = 'swedencentral'
param resourceGroupName string = ''

// Shared secret between the router proxy and the Function App (see function.bicep).
@secure()
param proxySharedSecret string = ''

// Site repo's platform storage account (stfsplat*) whose `retentionclock` table
// carries each competition's deletion verdict. Rows bound to a platform
// competition (PlatformId) are deleted only when it says so. The tool has no
// clock of its own: empty = bound rows are never deleted (and generation
// stores no date) — local dev only. The read grant comes from the site repo
// (TOOL_PRINCIPAL_ID_SCOREMODIFIER).
param platformStorageAccountName string = ''

// Nightly orphan step: true = delete orphaned records older than 60 days,
// false = dry run (logs only) — the kill switch (GitHub variable
// ORPHAN_PURGE_ENABLED=false). See modules/function.bicep.
param orphanPurgeEnabled bool = true

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
}

module storage 'modules/storage.bicep' = {
  scope: rg
  name: 'storageDeployment'
  params: {
    location: location
    storageAccountName: 'stfsscore${uniqueString(rg.id)}'
    containerName: 'fs-scoremodifier'
  }
}

module function 'modules/function.bicep' = {
  scope: rg
  name: 'functionDeployment'
  params: {
    location: location
    functionAppName: 'func-fs-scoremodifier-${uniqueString(rg.id)}'
    appServicePlanName: 'asp-fs-scoremodifier'
    appInsightsName: 'ai-fs-scoremodifier'
    // rg-fs-scoremodifier-<env> -> log-fs-scoremodifier-<env>
    logAnalyticsWorkspaceName: 'log-fs-scoremodifier-${last(split(resourceGroupName, '-'))}'
    storageAccountName: storage.outputs.storageAccountName
    deploymentContainerUrl: 'https://${storage.outputs.storageAccountName}.blob.${environment().suffixes.storage}/app-package'
    // No CORS: the browser never talks to the Function App directly, only the
    // router Web App in the site repo does (server-to-server, CORS not applied).
    allowedOrigins: []
    proxySharedSecret: proxySharedSecret
    platformStorageAccountName: platformStorageAccountName
    orphanPurgeEnabled: orphanPurgeEnabled
  }
}

module roleAssignment 'modules/roleassignment.bicep' = {
  scope: rg
  name: 'roleAssignmentDeployment'
  params: {
    storageAccountName: storage.outputs.storageAccountName
    functionPrincipalId: function.outputs.functionPrincipalId
  }
}

output resourceGroupName string = rg.name
output storageAccountName string = storage.outputs.storageAccountName
output functionAppName string = function.outputs.functionAppName
output orphanPurgeEnabled bool = orphanPurgeEnabled
// Consumed by the site repo (GitHub environment var TOOL_PRINCIPAL_ID_SCOREMODIFIER)
// to grant this Function App read access to the shared competition-data container.
output functionPrincipalId string = function.outputs.functionPrincipalId
