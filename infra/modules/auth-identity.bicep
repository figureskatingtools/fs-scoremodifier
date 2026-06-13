param location string
param managedIdentityName string

resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: managedIdentityName
  location: location
}

output clientId string = managedIdentity.properties.clientId
output principalId string = managedIdentity.properties.principalId
output resourceId string = managedIdentity.id
