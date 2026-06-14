using '../main.bicep'

param resourceGroupName = 'rg-fs-scoremodifier-prod'
param location = 'swedencentral'
// authClientId is injected from GitHub Environment secrets at deploy time
param customDomain = 'scoremodifier.figureskatingtools.com'
