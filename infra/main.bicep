// Premium vs Standard block blob performance demo.
//
// Deploys, in one region: a Premium (BlockBlobStorage) and a Standard (StorageV2) account, and a
// Linux VM that reaches both privately (private endpoints by default, or a service endpoint) and
// authenticates with its managed identity. The VM is bootstrapped with the blobperf tool (vm/) by the
// Custom Script Extension.
//
//   az deployment sub create -l swedencentral -f infra/main.bicep -p adminSshPublicKey="$(cat ~/.ssh/id_ed25519.pub)"
targetScope = 'subscription'

metadata description = 'Premium vs Standard block blob storage performance demo (storage accounts + benchmark VM)'

@description('Azure region for every resource. The VM and the storage accounts must share a region.')
param location string = deployment().location

@description('Resource group to create or update.')
param resourceGroupName string = 'rg-blobperf-demo'

@description('Prefix for resource names: 3-10 lowercase letters or digits.')
@minLength(3)
@maxLength(10)
param namePrefix string = 'blobperf'

@description('Benchmark VM size. The blog post used a D32 v3 (32 vCPU, 128 GiB, 16 Gbps); Standard_D32s_v6 has the same shape.')
param vmSize string = 'Standard_D32s_v6'

@description('Availability zones for benchmark VMs. Empty: one VM without a zone. For example [\'1\', \'2\', \'3\']: one identical VM per zone, all using the same storage accounts, to compare zones (scripts/demo.* zones).')
param vmZones array = []

@description('Admin user name of the VM.')
param adminUsername string = 'azureuser'

@description('SSH public key for the admin user. SSH is only reachable when sshSourceAddressPrefix is set; scripts/demo.* generate a throwaway key if you have none.')
param adminSshPublicKey string

@description('Optional CIDR allowed to SSH to the VM, e.g. 203.0.113.10/32. Empty: no public IP and no inbound access (use az vm run-command).')
param sshSourceAddressPrefix string = ''

@description('Replication of the Premium account. The blog post used LRS.')
@allowed([
  'Premium_LRS'
  'Premium_ZRS'
])
param premiumSkuName string = 'Premium_LRS'

@description('Replication of the Standard account. The blog post used LRS.')
@allowed([
  'Standard_LRS'
  'Standard_ZRS'
])
param standardSkuName string = 'Standard_LRS'

@description('How the VM reaches the storage accounts. PrivateEndpoint works even where policy disables public network access on storage; ServiceEndpoint uses the public endpoint over the Azure backbone, like the original 2018 test.')
@allowed([
  'PrivateEndpoint'
  'ServiceEndpoint'
])
param storageNetworkMode string = 'PrivateEndpoint'

@description('Turn Microsoft Defender for Storage off on the two test accounts (random data only). Otherwise on-upload malware scanning reads every uploaded blob during the tests and costs about $0.15/GB.')
param excludeFromDefenderForStorage bool = true

@description('If Microsoft Defender for Endpoint is installed on the VM, exclude the RAM-disk work folder (random data only) from real-time scanning so it does not slow down AzCopy.')
param mdeExcludeWorkFolder bool = true

@description('Daily VM auto-shutdown time (UTC, HHmm) to avoid paying for an idle VM. Empty disables it.')
param autoShutdownTimeUtc string = '2300'

@description('Tags for every resource.')
param tags object = {
  workload: 'blobperf-demo'
}

var suffix = take(uniqueString(subscription().id, resourceGroupName, location), 8)
var premiumAccountName = toLower('${namePrefix}prem${suffix}')
var standardAccountName = toLower('${namePrefix}std${suffix}')
var zones = empty(vmZones) ? [''] : vmZones
var vmNames = [for zone in zones: empty(zone) ? 'vm-${namePrefix}' : 'vm-${namePrefix}-z${zone}']

resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module network 'modules/network.bicep' = {
  scope: rg
  name: 'blobperf-network'
  params: {
    location: location
    namePrefix: namePrefix
    storageNetworkMode: storageNetworkMode
    sshSourceAddressPrefix: sshSourceAddressPrefix
    tags: tags
  }
}

module vm 'modules/vm.bicep' = [
  for (zone, i) in zones: {
    scope: rg
    name: 'blobperf-${vmNames[i]}'
    params: {
      location: location
      name: vmNames[i]
      zone: zone
      vmSize: vmSize
      adminUsername: adminUsername
      adminSshPublicKey: adminSshPublicKey
      subnetId: network.outputs.vmSubnetId
      enablePublicIp: !empty(sshSourceAddressPrefix)
      autoShutdownTimeUtc: autoShutdownTimeUtc
      tags: tags
    }
  }
]

module premium 'modules/storage.bicep' = {
  scope: rg
  name: 'blobperf-storage-premium'
  params: {
    name: premiumAccountName
    location: location
    kind: 'BlockBlobStorage'
    skuName: premiumSkuName
    networkMode: storageNetworkMode
    vmSubnetId: network.outputs.vmSubnetId
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.blobPrivateDnsZoneId
    principalIds: [for i in range(0, length(zones)): vm[i].outputs.principalId]
    excludeFromDefender: excludeFromDefenderForStorage
    tags: tags
  }
}

module standard 'modules/storage.bicep' = {
  scope: rg
  name: 'blobperf-storage-standard'
  params: {
    name: standardAccountName
    location: location
    kind: 'StorageV2'
    skuName: standardSkuName
    networkMode: storageNetworkMode
    vmSubnetId: network.outputs.vmSubnetId
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    privateDnsZoneId: network.outputs.blobPrivateDnsZoneId
    principalIds: [for i in range(0, length(zones)): vm[i].outputs.principalId]
    excludeFromDefender: excludeFromDefenderForStorage
    tags: tags
  }
}

// Runs after both accounts and role assignments exist, so each VM can verify access during setup.
module setup 'modules/vm-setup.bicep' = [
  for (zone, i) in zones: {
    scope: rg
    name: 'blobperf-setup-${vmNames[i]}'
    params: {
      location: location
      vmName: vm[i].outputs.name
      premiumAccountName: premium.outputs.name
      premiumBlobEndpoint: premium.outputs.blobEndpoint
      standardAccountName: standard.outputs.name
      standardBlobEndpoint: standard.outputs.blobEndpoint
      mdeExcludeWorkFolder: mdeExcludeWorkFolder
      tags: tags
    }
  }
]

output resourceGroupName string = rg.name
output vmNames array = [for i in range(0, length(zones)): vm[i].outputs.name]
output premiumAccountName string = premium.outputs.name
output standardAccountName string = standard.outputs.name
output storageNetworkMode string = storageNetworkMode
output vmPublicIpAddresses array = [for i in range(0, length(zones)): vm[i].outputs.publicIpAddress]
output natPublicIpAddress string = network.outputs.natPublicIpAddress
output setupScriptHash string = setup[0].outputs.scriptHash
