// Second PUT of the hostname binding to attach the managed certificate.
// Lives in its own module so webapp-customdomain.bicep can declare the
// binding twice (create, then enable SNI) across two deployments.
param webAppName string
param customDomain string
param certificateThumbprint string

resource webApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webAppName
}

resource hostnameBinding 'Microsoft.Web/sites/hostNameBindings@2023-12-01' = {
  parent: webApp
  name: customDomain
  properties: {
    sslState: 'SniEnabled'
    thumbprint: certificateThumbprint
  }
}
