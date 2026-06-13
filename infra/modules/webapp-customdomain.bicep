// Binds a custom domain to the web app with a free App Service managed
// certificate. Requires the DNS CNAME + asuid TXT records to already exist
// (see dns.bicep — main.bicep orders these via dependsOn).
//
// Three steps because of Azure's chicken-and-egg constraints:
//   1. hostname binding without SSL (requires DNS verification records)
//   2. managed certificate (requires the binding)
//   3. re-PUT the binding with the cert thumbprint to enable SNI — done in a
//      nested module (sni-enable.bicep) because the same resource cannot be
//      declared twice in one deployment.
param webAppName string
param customDomain string
param appServicePlanId string
param location string

resource webApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

resource hostnameBinding 'Microsoft.Web/sites/hostNameBindings@2023-12-01' = {
  parent: webApp
  name: customDomain
  properties: {
    siteName: webAppName
    hostNameType: 'Verified'
  }
}

resource managedCertificate 'Microsoft.Web/certificates@2023-12-01' = {
  name: 'cert-${customDomain}'
  location: location
  properties: {
    serverFarmId: appServicePlanId
    canonicalName: customDomain
  }
  dependsOn: [
    hostnameBinding
  ]
}

module sniEnable 'sni-enable.bicep' = {
  name: 'sniEnable-${uniqueString(customDomain)}'
  params: {
    webAppName: webAppName
    customDomain: customDomain
    certificateThumbprint: managedCertificate.properties.thumbprint
  }
}
