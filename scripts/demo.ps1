#Requires -Version 7.2
<#
.SYNOPSIS
    Deploy and run the Azure Premium vs Standard block blob performance demo.

.DESCRIPTION
    Thin wrapper around the Azure CLI. All measurements run on the benchmark VM (vm/blobperf.py).
    This script deploys infra/main.bicep, starts jobs on the VM with 'az vm run-command' (the VM
    needs no inbound network access), follows their progress and downloads the report.

    Commands:
      deploy      Deploy or update the resource group, network, storage accounts and VM.
      info        VM details and access to both storage accounts.
      run         Load the dataset if needed, run every test and print the summary (blobperf run-all).
      load        Load the 1,000,000 x 64 KiB latency dataset only (blobperf load).
      latency     Latency test only (blobperf latency).
      throughput  AzCopy upload/download and list test only (blobperf throughput).
      status      Progress of the current or last job.
      wait        Follow the current job until it finishes.
      report      Print the latest summary.
      download    Save the latest report (summary.md/.json, report.html, raw results) under ./results.
      exec        Run any blobperf command on the VM, e.g. -Options 'cleanup'.
      stop        Deallocate the VM (storage accounts and data are kept).
      start       Start the VM again.
      destroy     Delete the resource group and everything in it.

.EXAMPLE
    ./scripts/demo.ps1 deploy -Location swedencentral

.EXAMPLE
    ./scripts/demo.ps1 run

.EXAMPLE
    ./scripts/demo.ps1 latency -Options '--reads 2000'

.EXAMPLE
    ./scripts/demo.ps1 download
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('deploy', 'info', 'run', 'load', 'latency', 'throughput', 'status', 'wait', 'report',
        'download', 'exec', 'stop', 'start', 'destroy')]
    [string] $Command,

    # Resource group that holds (or will hold) the demo.
    [string] $ResourceGroup = 'rg-blobperf-demo',

    # Azure region for 'deploy'.
    [string] $Location = 'swedencentral',

    # VM size for 'deploy' (default: the template default, Standard_D32s_v6).
    [string] $VmSize,

    # SSH public key for 'deploy'. Default: ~/.ssh/id_ed25519.pub or id_rsa.pub, else a throwaway key.
    [string] $SshPublicKeyPath,

    # 'deploy' only: allow SSH from this CIDR, or 'me' for your current public IP. Default: no inbound access.
    [string] $AllowSshFrom,

    # Extra options for the blobperf command on the VM, e.g. '--iterations 1 --reads 2000'.
    [string] $Options = '',

    # Seconds between progress checks while a job runs.
    [int] $PollSeconds = 60,

    # Start the job and return without following it.
    [switch] $NoWait,

    # Do not ask for confirmation in 'destroy'.
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Blobperf = '/usr/local/bin/blobperf'
$script:VmName = $null

function Invoke-AzCli {
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..1] -join ' ') failed (exit code $LASTEXITCODE)." }
    $output
}

function Get-VmName {
    if (-not $script:VmName) {
        $script:VmName = Invoke-AzCli vm list --resource-group $ResourceGroup --query '[0].name' --output tsv
        if (-not $script:VmName) { throw "No VM in resource group '$ResourceGroup'. Run './scripts/demo.ps1 deploy' first." }
    }
    $script:VmName
}

# Runs a shell script on the VM as root and returns its stdout/stderr (run-command keeps ~4 KB of each).
function Invoke-OnVm([string] $Script) {
    $file = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($file.FullName, "#!/bin/bash`n$($Script -replace "`r`n", "`n")`n")
        $json = Invoke-AzCli vm run-command invoke --resource-group $ResourceGroup --name (Get-VmName) `
            --command-id RunShellScript --scripts "@$($file.FullName)" --output json
        $message = (($json -join "`n") | ConvertFrom-Json).value[0].message
        if ($message -match '(?s)\[stdout\]\r?\n(.*?)\[stderr\]\r?\n?(.*)$') {
            return [pscustomobject]@{ Stdout = $Matches[1].TrimEnd(); Stderr = $Matches[2].Trim() }
        }
        return [pscustomobject]@{ Stdout = $message; Stderr = '' }
    }
    finally {
        Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Write-VmOutput($Result) {
    if ($Result.Stdout) { Write-Host $Result.Stdout }
    if ($Result.Stderr) { Write-Host $Result.Stderr -ForegroundColor Yellow }
}

function Start-VmJob([string] $BlobperfCommand) {
    $result = Invoke-OnVm "$Blobperf start $BlobperfCommand $Options"
    Write-VmOutput $result
    if ($result.Stdout -notmatch 'started job') { throw 'The job did not start (see the output above).' }
}

function Wait-VmJob {
    $seen = [System.Collections.Generic.HashSet[string]]::new()
    while ($true) {
        $lines = (Invoke-OnVm "$Blobperf status --lines 12").Stdout -split "`r?`n"
        $state = if ($lines[0] -match 'STATE:\s*(\S+)') { $Matches[1] } else { 'unknown' }
        foreach ($line in $lines | Select-Object -Skip 1) {
            if ($line.Trim() -and $seen.Add($line)) { Write-Host $line }
        }
        if ($state -ne 'running') {
            Write-Host $lines[0]
            return $state
        }
        Start-Sleep -Seconds $PollSeconds
    }
}

function Invoke-VmJob([string] $BlobperfCommand, [switch] $Report) {
    Start-VmJob $BlobperfCommand
    if ($NoWait) {
        Write-Host "Follow it with: ./scripts/demo.ps1 wait -ResourceGroup $ResourceGroup"
        return
    }
    $state = Wait-VmJob
    if ($state -ne 'succeeded') { throw "The job finished with state '$state'. See './scripts/demo.ps1 status'." }
    if ($Report) { Write-VmOutput (Invoke-OnVm "$Blobperf report --print md") }
}

function Save-Report {
    $chunks = [System.Collections.Generic.List[string]]::new()
    $total, $sha = 1, ''
    for ($i = 0; $i -lt $total; $i++) {
        $out = (Invoke-OnVm "$Blobperf export --chunk $i").Stdout
        $header, $body = $out -split "`r?`n", 2
        if ($header -notmatch 'BLOBPERF-EXPORT chunk=\d+ total=(\d+) sha256=([0-9a-f]{64})') {
            throw "Unexpected export output: $out"
        }
        $total, $sha = [int]$Matches[1], $Matches[2]
        $chunks.Add(($body -replace '\s', ''))
        Write-Host "  downloaded part $($i + 1)/$total"
    }
    $bytes = [Convert]::FromBase64String(-join $chunks)
    $actual = -join ([Security.Cryptography.SHA256]::HashData($bytes) | ForEach-Object { $_.ToString('x2') })
    if ($actual -ne $sha) { throw 'Checksum mismatch in the downloaded report.' }
    $destination = Join-Path $RepoRoot ('results/{0:yyyyMMdd-HHmmss}' -f (Get-Date))
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $archive = Join-Path $destination 'bundle.tgz'
    [IO.File]::WriteAllBytes($archive, $bytes)
    tar -xzf $archive -C $destination
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the report bundle.' }
    Remove-Item $archive
    Write-Host "Saved to $destination"
    Get-ChildItem $destination | ForEach-Object { Write-Host "  $($_.Name)" }
}

# Public half of a fresh RSA key whose private half is discarded. Azure needs an admin credential for a
# Linux VM; when SSH is not used, a key nobody holds is the safest one.
function New-ThrowawaySshKey {
    $rsa = [Security.Cryptography.RSA]::Create(3072)
    try {
        $key = $rsa.ExportParameters($false)
        $stream = [IO.MemoryStream]::new()
        $parts = @([Text.Encoding]::ASCII.GetBytes('ssh-rsa'), $key.Exponent, $key.Modulus)
        for ($i = 0; $i -lt $parts.Count; $i++) {
            [byte[]] $bytes = $parts[$i]
            if ($i -gt 0 -and ($bytes[0] -band 0x80)) { $bytes = [byte[]] (@(0) + $bytes) }
            $length = [BitConverter]::GetBytes([uint32] $bytes.Length)
            if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($length) }
            $stream.Write($length, 0, 4)
            $stream.Write($bytes, 0, $bytes.Length)
        }
        return "ssh-rsa $([Convert]::ToBase64String($stream.ToArray())) blobperf-throwaway"
    }
    finally {
        $rsa.Dispose()
    }
}

function Get-SshPublicKey {
    # Azure cannot change the key of an existing VM by redeploying, so keep whatever it already has.
    $existing = az vm list --resource-group $ResourceGroup --query '[0].osProfile.linuxConfiguration.ssh.publicKeys[0].keyData' --output tsv 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        if ($SshPublicKeyPath) { Write-Warning 'The VM already exists and keeps its current SSH key (change it with az vm user update).' }
        Write-Host 'Reusing the SSH key of the existing VM.'
        return "$existing".Trim()
    }
    if ($SshPublicKeyPath) { return (Get-Content -Raw $SshPublicKeyPath).Trim() }
    foreach ($name in 'id_ed25519.pub', 'id_rsa.pub', 'id_ecdsa.pub') {
        $path = Join-Path $HOME ".ssh/$name"
        if (Test-Path $path) {
            Write-Host "Using SSH public key $path"
            return (Get-Content -Raw $path).Trim()
        }
    }
    if ($AllowSshFrom) { throw 'SSH access requested but no public key found; pass -SshPublicKeyPath.' }
    Write-Host 'No SSH key found: using a throwaway key (the VM is managed with run-command only).'
    New-ThrowawaySshKey
}

function Invoke-Deploy {
    $parameters = [ordered]@{
        location          = @{ value = $Location }
        resourceGroupName = @{ value = $ResourceGroup }
        adminSshPublicKey = @{ value = (Get-SshPublicKey) }
    }
    if ($VmSize) { $parameters.vmSize = @{ value = $VmSize } }
    if ($AllowSshFrom) {
        $cidr = if ($AllowSshFrom -eq 'me') { "$((Invoke-RestMethod -Uri 'https://api.ipify.org').Trim())/32" } else { $AllowSshFrom }
        Write-Host "SSH will be allowed from $cidr"
        $parameters.sshSourceAddressPrefix = @{ value = $cidr }
    }
    $file = New-TemporaryFile
    try {
        @{
            '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
            contentVersion = '1.0.0.0'
            parameters     = $parameters
        } | ConvertTo-Json -Depth 5 | Set-Content -Path $file.FullName -Encoding utf8NoBOM
        $account = Invoke-AzCli account show --query '{name:name, id:id}' --output json | ConvertFrom-Json
        Write-Host "Deploying to subscription '$($account.name)' ($($account.id)), region $Location, resource group $ResourceGroup."
        Write-Host 'This takes about 5-10 minutes...'
        $outputs = Invoke-AzCli deployment sub create --name "blobperf-$ResourceGroup" --location $Location `
            --template-file (Join-Path $RepoRoot 'infra/main.bicep') --parameters "@$($file.FullName)" `
            --query properties.outputs --output json | ConvertFrom-Json
    }
    finally {
        Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
    }
    Write-Host ''
    Write-Host "VM:                $($outputs.vmName.value)"
    Write-Host "Premium account:   $($outputs.premiumAccountName.value)"
    Write-Host "Standard account:  $($outputs.standardAccountName.value)"
    if ($outputs.vmPublicIpAddress.value) { Write-Host "SSH:               ssh azureuser@$($outputs.vmPublicIpAddress.value)" }
    Write-Host ''
    Write-Host 'Next: ./scripts/demo.ps1 run    (loads the dataset on first use, then runs every test)'
}

switch ($Command) {
    'deploy' { Invoke-Deploy }
    'info' { Write-VmOutput (Invoke-OnVm "$Blobperf info") }
    'run' { Invoke-VmJob 'run-all' -Report }
    'load' { Invoke-VmJob 'load' }
    'latency' { Invoke-VmJob 'latency' -Report }
    'throughput' { Invoke-VmJob 'throughput' -Report }
    'status' { Write-VmOutput (Invoke-OnVm "$Blobperf status --lines 25") }
    'wait' {
        $state = Wait-VmJob
        if ($state -ne 'succeeded') { exit 1 }
    }
    'report' { Write-VmOutput (Invoke-OnVm "$Blobperf report --print md") }
    'download' { Save-Report }
    'exec' {
        if (-not $Options) { throw "Pass the blobperf command with -Options, e.g. -Options 'cleanup'." }
        Write-VmOutput (Invoke-OnVm "$Blobperf $Options")
    }
    'stop' { Invoke-AzCli vm deallocate --resource-group $ResourceGroup --name (Get-VmName) | Out-Null; Write-Host 'VM deallocated.' }
    'start' { Invoke-AzCli vm start --resource-group $ResourceGroup --name (Get-VmName) | Out-Null; Write-Host 'VM started.' }
    'destroy' {
        if (-not $Yes) {
            $answer = Read-Host "Delete resource group '$ResourceGroup' and everything in it? (yes/no)"
            if ($answer -ne 'yes') { Write-Host 'Cancelled.'; return }
        }
        Invoke-AzCli group delete --name $ResourceGroup --yes --no-wait | Out-Null
        Write-Host "Deleting resource group '$ResourceGroup' in the background."
    }
}
