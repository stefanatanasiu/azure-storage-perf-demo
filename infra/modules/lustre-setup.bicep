// Installs the Lustre client, its configuration and lustreperf on one benchmark VM, with a managed Run Command.
//
// Like vm-setup.bicep, the tooling travels inside the deployment: vm/lustre-install.sh with vm/lustreperf.py
// embedded in a quoted heredoc. ARM caps any evaluated string at 131,072 characters (the CI workflow checks
// it). If the running kernel has no Lustre client module, the script switches kernels and reboots the VM
// shortly after it reports success; lustreperf mounts the file system on first use.

@description('Azure region.')
param location string

@description('Name of the benchmark VM.')
param vmName string

@description('Name of the Azure Managed Lustre file system.')
param fileSystemName string

@description('IP address of the file system\'s management service (MGS).')
param mgsAddress string

@description('Mount command that the file system reports, e.g. "mount -t lustre -o noatime,user_xattr 10.0.0.4@tcp0:/lustrefs /<local path>".')
param mountCommand string

@description('Lustre version of the file system, e.g. 2.15.')
param lustreVersion string

@description('AMLFS SKU.')
param skuName string

@description('Capacity in TiB.')
param storageCapacityTiB int

@description('Availability zone of the file system.')
param zone string

@description('Exclude the mount point from Defender for Endpoint real-time scanning if MDE is installed.')
param mdeExcludeMount bool

@description('Tags for every resource.')
param tags object

var installTemplate = loadTextContent('../../vm/lustre-install.sh')
var toolSource = loadTextContent('../../vm/lustreperf.py')
var placeholders = {
  __FILESYSTEM__: fileSystemName
  __MGS_ADDRESS__: mgsAddress
  __MOUNT_COMMAND__: mountCommand
  __LUSTRE_VERSION__: lustreVersion
  __SKU__: skuName
  __CAPACITY_TIB__: string(storageCapacityTiB)
  __ZONE__: zone
  __MDE_EXCLUDE_MOUNT__: mdeExcludeMount ? 'true' : 'false'
}
var configured = reduce(items(placeholders), installTemplate, (text, placeholder) => replace(text, placeholder.key, placeholder.value))
// The tool goes in last so the placeholder replacements never scan its source.
var script = replace(configured, '__LUSTREPERF_PY__', toolSource)

resource vm 'Microsoft.Compute/virtualMachines@2025-04-01' existing = {
  name: vmName
}

resource setup 'Microsoft.Compute/virtualMachines/runCommands@2025-04-01' = {
  parent: vm
  name: 'lustre-setup'
  location: location
  tags: tags
  properties: {
    source: {
      script: script
    }
    asyncExecution: false
    treatFailureAsDeploymentFailure: true
    timeoutInSeconds: 1800
  }
}

output scriptHash string = uniqueString(script)
