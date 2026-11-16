using 'main.bicep'

// Example parameters for deploying by hand (scripts/demo.ps1 and scripts/demo.sh pass parameters directly):
//
//   export BLOBPERF_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
//   az deployment sub create --location swedencentral --template-file infra/main.bicep --parameters infra/main.bicepparam
//
// Keep environment-specific copies as *.local.bicepparam (ignored by git).

param location = 'swedencentral'
param resourceGroupName = 'rg-blobperf-demo'
param vmSize = 'Standard_D32s_v6'
param adminSshPublicKey = readEnvironmentVariable('BLOBPERF_SSH_PUBLIC_KEY')
