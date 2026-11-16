// Benchmark VM: Ubuntu 24.04 with accelerated networking and a system-assigned managed identity.
//
// No public IP unless SSH is enabled; the demo scripts drive the VM with `az vm run-command`.

@description('Azure region.')
param location string

@description('Prefix for resource names.')
param namePrefix string

@description('VM size.')
param vmSize string

@description('Admin user name.')
param adminUsername string

@description('SSH public key for the admin user.')
param adminSshPublicKey string

@description('Resource ID of the VM subnet.')
param subnetId string

@description('Create a public IP for SSH (the NSG only admits the configured source prefix).')
param enablePublicIp bool

@description('Daily auto-shutdown time (UTC, HHmm). Empty disables it.')
param autoShutdownTimeUtc string

@description('Tags for every resource.')
param tags object

var vmName = 'vm-${namePrefix}'

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-10-01' = if (enablePublicIp) {
  name: 'pip-${vmName}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource nic 'Microsoft.Network/networkInterfaces@2024-10-01' = {
  name: 'nic-${vmName}'
  location: location
  tags: tags
  dependsOn: [
    publicIp
  ]
  properties: {
    enableAcceleratedNetworking: true
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: union(
          {
            privateIPAllocationMethod: 'Dynamic'
            subnet: {
              id: subnetId
            }
          },
          enablePublicIp ? { publicIPAddress: { id: resourceId('Microsoft.Network/publicIPAddresses', 'pip-${vmName}') } } : {}
        )
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2025-04-01' = {
  name: vmName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        name: 'osdisk-${vmName}'
        createOption: 'FromImage'
        caching: 'ReadWrite'
        deleteOption: 'Delete'
        diskSizeGB: 64
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshPublicKey
            }
          ]
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

// Always deployed: clearing the time disables an existing schedule (incremental deployments never delete resources).
resource autoShutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  tags: tags
  properties: {
    status: empty(autoShutdownTimeUtc) ? 'Disabled' : 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: {
      time: empty(autoShutdownTimeUtc) ? '2300' : autoShutdownTimeUtc
    }
    timeZoneId: 'UTC'
    targetResourceId: vm.id
    notificationSettings: {
      status: 'Disabled'
    }
  }
}

output name string = vm.name
output principalId string = vm.identity.principalId
output publicIpAddress string = enablePublicIp ? publicIp!.properties.ipAddress : ''
