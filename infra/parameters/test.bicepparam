using '../main.bicep'

param resourceGroupName = 'rg-fs-scoremodifier-test'
param location = 'swedencentral'
// authClientId is injected from GitHub Environment secrets at deploy time
param customDomain = 'test.scoremodifier.figureskatingtools.com'
