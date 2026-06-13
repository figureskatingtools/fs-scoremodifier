param location string
param webAppName string
param appServicePlanName string
param skuName string = 'B1'
param skuTier string = 'Basic'
param authClientId string = ''
param authManagedIdentityClientId string = ''
param authManagedIdentityResourceId string = ''
param tenantId string = subscription().tenantId

resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: appServicePlanName
  location: location
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    reserved: true
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = {
  name: webAppName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${authManagedIdentityResourceId}': {}
    }
  }
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'NODE|22-lts'
      appCommandLine: 'node server.js'
    }
    httpsOnly: true
  }
}

resource authConfig 'Microsoft.Web/sites/config@2022-09-01' = if (!empty(authClientId)) {
  parent: webApp
  name: 'authsettingsV2'
  properties: {
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: authClientId
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            authClientId
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

resource authAppSettings 'Microsoft.Web/sites/config@2022-09-01' = if (!empty(authClientId)) {
  parent: webApp
  name: 'appsettings'
  properties: {
    OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID: authManagedIdentityClientId
  }
}

output webAppName string = webApp.name
output webAppDefaultHostName string = webApp.properties.defaultHostName
output customDomainVerificationId string = webApp.properties.customDomainVerificationId
output appServicePlanId string = appServicePlan.id
