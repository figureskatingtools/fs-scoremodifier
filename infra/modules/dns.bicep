// DNS records for a web app custom domain in an existing DNS zone.
// Deployed cross-resource-group: the zone lives in its own RG (managed by the
// root frontend site deployment), this module only adds/updates record sets.
param dnsZoneName string
param recordName string
param targetHostname string
param domainVerificationId string

resource dnsZone 'Microsoft.Network/dnsZones@2023-07-01-preview' existing = {
  name: dnsZoneName
}

// CNAME pointing the custom domain at the web app's default hostname
resource cnameRecord 'Microsoft.Network/dnsZones/CNAME@2023-07-01-preview' = {
  parent: dnsZone
  name: recordName
  properties: {
    TTL: 3600
    CNAMERecord: {
      cname: targetHostname
    }
  }
}

// TXT record App Service uses to verify domain ownership before binding
resource asuidRecord 'Microsoft.Network/dnsZones/TXT@2023-07-01-preview' = {
  parent: dnsZone
  name: 'asuid.${recordName}'
  properties: {
    TTL: 3600
    TXTRecords: [
      {
        value: [
          domainVerificationId
        ]
      }
    ]
  }
}
