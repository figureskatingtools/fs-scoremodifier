using '../main.bicep'

param resourceGroupName = 'rg-fs-scoremodifier-prod'
param location = 'swedencentral'
// proxySharedSecret is injected from GitHub Environment secrets at deploy time.
// No frontend/auth/DNS params: the UI is served by figureskatingtools-site.
