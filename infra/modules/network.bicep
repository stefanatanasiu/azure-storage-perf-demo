// Virtual network for the benchmark VM.
//
// storageNetworkMode decides how the VM reaches the storage accounts:
//   PrivateEndpoint  (default) private endpoints in snet-pe + the privatelink.blob DNS zone. Works where
//                    policy forces "public network access: disabled" on storage accounts.
//   ServiceEndpoint  Microsoft.Storage service endpoint on snet-vm: the public endpoint over the Azure
//                    backbone, the path the original 2018 test used.
// The NAT gateway is only the explicit outbound path for downloading AzCopy (new subnets are private,
// without default outbound access); storage traffic does not use it in either mode.

@description('Azure region.')
param location string

@description('Prefix for resource names.')
param namePrefix string

@description('How the VM reaches the storage accounts: PrivateEndpoint or ServiceEndpoint.')
@allowed([
  'PrivateEndpoint'
  'ServiceEndpoint'
])
param storageNetworkMode string

@description('Optional CIDR allowed to SSH to the VM. Empty: no inbound rule.')
param sshSourceAddressPrefix string

@description('Tags for every resource.')
param tags object

var usePrivateEndpoints = storageNetworkMode == 'PrivateEndpoint'

resource natPublicIp 'Microsoft.Network/publicIPAddresses@2024-10-01' = {
  name: 'pip-${namePrefix}-nat'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-10-01' = {
  name: 'ng-${namePrefix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 4
    publicIpAddresses: [
      {
        id: natPublicIp.id
      }
    ]
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-10-01' = {
  name: 'nsg-${namePrefix}-vm'
  location: location
  tags: tags
  properties: {
    securityRules: empty(sshSourceAddressPrefix)
      ? []
      : [
          {
            name: 'Allow-SSH-From-Admin'
            properties: {
              priority: 100
              direction: 'Inbound'
              access: 'Allow'
              protocol: 'Tcp'
              sourceAddressPrefix: sshSourceAddressPrefix
              sourcePortRange: '*'
              destinationAddressPrefix: '*'
              destinationPortRange: '22'
            }
          }
        ]
  }
}

// Default rules only; many organizations require an NSG on every subnet.
resource privateEndpointNsg 'Microsoft.Network/networkSecurityGroups@2024-10-01' = {
  name: 'nsg-${namePrefix}-pe'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-10-01' = {
  name: 'vnet-${namePrefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/24'
      ]
    }
    subnets: [
      {
        name: 'snet-vm'
        properties: union(
          {
            addressPrefix: '10.42.0.0/26'
            defaultOutboundAccess: false
            natGateway: {
              id: natGateway.id
            }
            networkSecurityGroup: {
              id: nsg.id
            }
          },
          usePrivateEndpoints
            ? {}
            : {
                serviceEndpoints: [
                  {
                    service: 'Microsoft.Storage'
                    locations: [
                      location
                    ]
                  }
                ]
              }
        )
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.42.0.64/27'
          defaultOutboundAccess: false
          networkSecurityGroup: {
            id: privateEndpointNsg.id
          }
        }
      }
    ]
  }
}

resource blobDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (usePrivateEndpoints) {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource blobDnsZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (usePrivateEndpoints) {
  parent: blobDnsZone
  name: 'link-${vnet.name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output vmSubnetId string = vnet.properties.subnets[0].id
output privateEndpointSubnetId string = vnet.properties.subnets[1].id
output blobPrivateDnsZoneId string = usePrivateEndpoints ? blobDnsZone.id : ''
output natPublicIpAddress string = natPublicIp.properties.ipAddress
