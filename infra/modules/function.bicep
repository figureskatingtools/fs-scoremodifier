param location string
param functionAppName string
param appServicePlanName string
param appInsightsName string

// Technical logs are kept 90 days (the site's privacy notice says so). A
// classic component's retention follows the Log Analytics workspace it is
// linked to, so both the workspace and the component say 90 explicitly.
@description('Log Analytics workspace behind the App Insights component (log-fs-<tool>-<env>).')
param logAnalyticsWorkspaceName string

@description('Days the technical logs are kept (workspace and App Insights). The privacy notice promises 90.')
@minValue(30)
@maxValue(90)
param logRetentionDays int = 90
param storageAccountName string
param deploymentContainerUrl string
// Expected to stay empty: the browser never calls the Function App directly,
// only the router Web App (figureskatingtools-site repo) proxies to it, and
// server-to-server calls are not subject to CORS.
param allowedOrigins array = []

// Shared secret the router proxy sends as X-Proxy-Secret. Empty = the
// function doesn't enforce it (local/dev). See function_app.py:_proxy_secret_ok
// and PROXY-CONTRACT.md.
@secure()
param proxySharedSecret string = ''

// Platform (site repo) storage account whose `retentionclock` table bound rows
// follow (platform_clock.py). Empty = bound rows are never deleted (no local clock).
param platformStorageAccountName string = ''

// Kill switch of the nightly orphan step (function_app.py _run_cleanup_sweep):
// true = an orphaned record (no platform competition behind it) older than 60
// days is deleted like a platform "deleted" verdict; false = dry run, the
// timer only logs what it would delete. Bicep-owned, so it survives redeploys.
@description('true = the nightly sweep deletes orphaned records older than 60 days; false = dry-run logging only.')
param orphanPurgeEnabled bool = true

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
  }
}

// Workspace-based: linking it here replaces the default workspace the classic
// component was auto-migrated to. Data already in that old workspace keeps its
// retention until it ages out.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    RetentionInDays: logRetentionDays
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      cors: {
        allowedOrigins: allowedOrigins
      }
      // Explicitly no inbound IP restrictions. The endpoint must stay reachable
      // by the router proxy AND by the CI deploy's sync-triggers/health-check
      // (a Deny lock 403s the GitHub runner and hangs the pipeline). An empty
      // array also clears any restriction left over from a prior deploy.
      ipSecurityRestrictions: []
      ipSecurityRestrictionsDefaultAction: 'Allow'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storageAccountName
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'PROXY_SHARED_SECRET'
          value: proxySharedSecret
        }
        {
          name: 'PLATFORM_STORAGE_ACCOUNT'
          value: platformStorageAccountName
        }
        {
          name: 'ORPHAN_PURGE_ENABLED'
          value: orphanPurgeEnabled ? 'true' : 'false'
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: deploymentContainerUrl
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 100
        instanceMemoryMB: 2048
      }
    }
  }
}

resource authSettings 'Microsoft.Web/sites/config@2022-03-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    // The router Web App (figureskatingtools-site) handles the real Entra login
    // and proxies requests here, forwarding the user's email. The Function App
    // must allow anonymous so those proxied requests reach the app, which
    // authorizes via the forwarded header (get_user_email_from_header) after
    // the X-Proxy-Secret check. No identity provider is configured here — the
    // Entra app registration and its federated credential live with the router.
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

output functionAppName string = functionApp.name
output functionAppId string = functionApp.id
output functionPrincipalId string = functionApp.identity.principalId
