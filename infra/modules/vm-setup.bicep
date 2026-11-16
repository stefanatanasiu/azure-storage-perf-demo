// Bootstraps a benchmark VM with a managed Run Command.
//
// The tooling travels inside the deployment (vm/install.sh with vm/blobperf.py embedded in a quoted
// heredoc), so no repository clone, script storage or SAS token is needed. A Run Command takes the
// script as plain text: ARM caps any evaluated string at 131,072 characters, so install.sh plus
// blobperf.py must stay below that (the CI workflow checks it). Changing either file and redeploying
// runs the setup again.

@description('Azure region.')
param location string

@description('Name of the benchmark VM.')
param vmName string

@description('Premium storage account name.')
param premiumAccountName string

@description('Premium blob endpoint, e.g. https://<account>.blob.core.windows.net/.')
param premiumBlobEndpoint string

@description('Standard storage account name.')
param standardAccountName string

@description('Standard blob endpoint.')
param standardBlobEndpoint string

@description('Exclude the RAM-disk work folder from Defender for Endpoint real-time scanning if MDE is installed.')
param mdeExcludeWorkFolder bool

@description('Tags for every resource.')
param tags object

var installTemplate = loadTextContent('../../vm/install.sh')
var toolSource = loadTextContent('../../vm/blobperf.py')
var placeholders = {
  __PREMIUM_ACCOUNT__: premiumAccountName
  __PREMIUM_ENDPOINT__: premiumBlobEndpoint
  __STANDARD_ACCOUNT__: standardAccountName
  __STANDARD_ENDPOINT__: standardBlobEndpoint
  __MDE_EXCLUDE_WORKDIR__: mdeExcludeWorkFolder ? 'true' : 'false'
}
var configured = reduce(items(placeholders), installTemplate, (text, placeholder) => replace(text, placeholder.key, placeholder.value))
// The tool goes in last so the placeholder replacements never scan its source.
var script = replace(configured, '__BLOBPERF_PY__', toolSource)

resource vm 'Microsoft.Compute/virtualMachines@2025-04-01' existing = {
  name: vmName
}

resource setup 'Microsoft.Compute/virtualMachines/runCommands@2025-04-01' = {
  parent: vm
  name: 'blobperf-setup'
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
