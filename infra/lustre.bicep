// Optional Azure Managed Lustre (AMLFS) file system for the performance demo, plus the Lustre client and
// the lustreperf tool on every benchmark VM.
//
// Deploy it into the resource group of infra/main.bicep: it uses that deployment's VNet (subnet
// snet-lustre) and VMs, which must be running.
//
//   az deployment group create -g rg-blobperf-demo -f infra/lustre.bicep \
//     -p vmNames='["vm-blobperf-z1","vm-blobperf-z2","vm-blobperf-z3"]'
//
// AMLFS bills the provisioned capacity per hour for as long as the file system exists: the smallest one
// (AMLFS-Durable-Premium-500, 4 TiB) costs about $60 a day. Delete it as soon as the tests are done
// (scripts/demo.* lustre-delete, or az resource delete).
targetScope = 'resourceGroup'

metadata description = 'Azure Managed Lustre file system for the storage performance demo, with the Lustre client on each VM'

@description('Azure region. Must be the region of the VNet and the VMs.')
param location string = resourceGroup().location

@description('Prefix that infra/main.bicep used for resource names.')
@minLength(3)
@maxLength(10)
param namePrefix string = 'blobperf'

@description('AMLFS SKU: provisioned throughput per TiB of capacity. Premium-500 has the most bandwidth per TiB and the smallest (cheapest) minimum size, 4 TiB.')
@allowed([
  'AMLFS-Durable-Premium-40'
  'AMLFS-Durable-Premium-125'
  'AMLFS-Durable-Premium-250'
  'AMLFS-Durable-Premium-500'
])
param skuName string = 'AMLFS-Durable-Premium-500'

@description('Capacity in TiB, a multiple of the SKU increment: Premium-500 4, Premium-250 8, Premium-125 16, Premium-40 48.')
@minValue(4)
param storageCapacityTiB int = 4

@description('Availability zone of the file system: AMLFS is zonal. A VM in the same zone should see the lowest latency.')
@allowed([
  '1'
  '2'
  '3'
])
param zone string = '1'

@description('Benchmark VMs that get the Lustre client and lustreperf. They must be running.')
@minLength(1)
param vmNames array

@description('Day of the weekly 30-minute maintenance window.')
@allowed([
  'Monday'
  'Tuesday'
  'Wednesday'
  'Thursday'
  'Friday'
  'Saturday'
  'Sunday'
])
param maintenanceDayOfWeek string = 'Sunday'

@description('Start of the weekly maintenance window (UTC, HH:MM).')
param maintenanceTimeOfDayUtc string = '03:00'

@description('If Microsoft Defender for Endpoint is installed on the VMs, exclude the mount point (random test data only) from real-time scanning.')
param mdeExcludeMount bool = true

@description('Tags for every resource.')
param tags object = {
  workload: 'blobperf-demo'
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-10-01' existing = {
  name: 'vnet-${namePrefix}'
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-10-01' existing = {
  parent: vnet
  name: 'snet-lustre'
}

resource amlfs 'Microsoft.StorageCache/amlFilesystems@2026-08-01' = {
  name: 'amlfs-${namePrefix}'
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  zones: [
    zone
  ]
  properties: {
    filesystemSubnet: subnet.id
    storageCapacityTiB: storageCapacityTiB
    maintenanceWindow: {
      dayOfWeek: maintenanceDayOfWeek
      timeOfDayUTC: maintenanceTimeOfDayUtc
    }
  }
}

// Runs once the file system exists, because the client configuration needs its address.
module setup 'modules/lustre-setup.bicep' = [
  for vmName in vmNames: {
    name: 'lustre-setup-${vmName}'
    params: {
      location: location
      vmName: vmName
      fileSystemName: amlfs.name
      mgsAddress: amlfs.properties.clientInfo.mgsAddress
      mountCommand: amlfs.properties.clientInfo.mountCommand
      lustreVersion: amlfs.properties.clientInfo.lustreVersion
      skuName: skuName
      storageCapacityTiB: storageCapacityTiB
      zone: zone
      mdeExcludeMount: mdeExcludeMount
      tags: tags
    }
  }
]

output fileSystemName string = amlfs.name
output fileSystemId string = amlfs.id
output zone string = zone
output mgsAddress string = amlfs.properties.clientInfo.mgsAddress
output mountCommand string = amlfs.properties.clientInfo.mountCommand
output lustreVersion string = amlfs.properties.clientInfo.lustreVersion
output setupScriptHash string = setup[0].outputs.scriptHash
