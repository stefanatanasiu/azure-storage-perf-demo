// One test storage account: Premium (BlockBlobStorage) or Standard (StorageV2).
//
// Both accounts are configured identically apart from kind/SKU: Entra ID (OAuth) access only, no
// public blob access, reachable only from the VMs (private endpoint, or service endpoint rule), and no
// soft delete so deleted test data does not linger. Every benchmark VM's managed identity gets Storage
// Blob Data Contributor.

@description('Storage account name (3-24 lowercase letters and digits, globally unique).')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region.')
param location string

@description('BlockBlobStorage for Premium block blobs, StorageV2 for Standard general-purpose v2.')
@allowed([
  'BlockBlobStorage'
  'StorageV2'
])
param kind string

@description('SKU, for example Premium_LRS or Standard_LRS.')
param skuName string

@description('How the VM reaches the account: PrivateEndpoint or ServiceEndpoint.')
@allowed([
  'PrivateEndpoint'
  'ServiceEndpoint'
])
param networkMode string

@description('Resource ID of the VM subnet (allowed by the firewall in ServiceEndpoint mode).')
param vmSubnetId string

@description('Resource ID of the subnet for the private endpoint (PrivateEndpoint mode).')
param privateEndpointSubnetId string

@description('Resource ID of the privatelink.blob private DNS zone (PrivateEndpoint mode).')
param privateDnsZoneId string

@description('Object IDs of the VM managed identities (one per benchmark VM).')
param principalIds array

@description('Turn Microsoft Defender for Storage off for this account (overrides the subscription setting).')
param excludeFromDefender bool

@description('Tags for every resource.')
param tags object

var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var usePrivateEndpoint = networkMode == 'PrivateEndpoint'

resource account 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: name
  location: location
  tags: tags
  kind: kind
  sku: {
    name: skuName
  }
  properties: union(
    {
      allowBlobPublicAccess: false
      allowCrossTenantReplication: false
      allowSharedKeyAccess: false
      defaultToOAuthAuthentication: true
      minimumTlsVersion: 'TLS1_2'
      supportsHttpsTrafficOnly: true
      publicNetworkAccess: usePrivateEndpoint ? 'Disabled' : 'Enabled'
      networkAcls: {
        bypass: 'AzureServices'
        defaultAction: 'Deny'
        virtualNetworkRules: usePrivateEndpoint
          ? []
          : [
              {
                id: vmSubnetId
                action: 'Allow'
              }
            ]
      }
    },
    kind == 'StorageV2' ? { accessTier: 'Hot' } : {}
  )
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: account
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: false
    }
    containerDeleteRetentionPolicy: {
      enabled: false
    }
  }
}

resource latencyContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: 'latency'
  properties: {
    publicAccess: 'None'
  }
}

resource blobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in principalIds: {
    name: guid(account.id, principalId, storageBlobDataContributorRoleId)
    scope: account
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
      principalId: principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-10-01' = if (usePrivateEndpoint) {
  name: 'pe-${name}-blob'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    customNetworkInterfaceName: 'nic-pe-${name}-blob'
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: account.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-10-01' = if (usePrivateEndpoint) {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: privateDnsZoneId
        }
      }
    ]
  }
}

resource defenderForStorage 'Microsoft.Security/defenderForStorageSettings@2025-01-01' = if (excludeFromDefender) {
  name: 'current'
  scope: account
  properties: {
    isEnabled: false
    overrideSubscriptionLevelSettings: true
    malwareScanning: {
      onUpload: {
        isEnabled: false
      }
    }
    sensitiveDataDiscovery: {
      isEnabled: false
    }
  }
}

output name string = account.name
output blobEndpoint string = account.properties.primaryEndpoints.blob
