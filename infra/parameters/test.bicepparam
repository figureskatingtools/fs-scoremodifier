using '../main.bicep'

param resourceGroupName = 'rg-fs-scoremodifier-test'
param location = 'swedencentral'
// proxySharedSecret is injected from GitHub Environment secrets at deploy time,
// platformStorageAccountName from the environment variable PLATFORM_STORAGE_ACCOUNT.
// No frontend/auth/DNS params: the UI is served by figureskatingtools-site.
