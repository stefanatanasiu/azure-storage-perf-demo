#Requires -Version 7.2
<#
.SYNOPSIS
    Deploy and run the Azure Premium vs Standard block blob performance demo.

.DESCRIPTION
    Thin wrapper around the Azure CLI. All measurements run on the benchmark VM(s) (vm/blobperf.py).
    This script deploys infra/main.bicep, starts jobs on the VMs with 'az vm run-command' (the VMs
    need no inbound network access), follows their progress and downloads the report.

    Commands:
      deploy      Deploy or update the resource group, network, storage accounts and VM(s).
                  -Zones 1,2,3 deploys one VM per availability zone (see 'zones').
      info        VM details and access to both storage accounts (every VM).
      run         Load the dataset if needed, run every test and print the summary (blobperf run-all).
      load        Load the 1,000,000 x 64 KiB latency dataset only (blobperf load).
      latency     Latency test only (blobperf latency).
      throughput  AzCopy upload/download and list test only (blobperf throughput).
      zones       Compare availability zones: run the tests on each zonal VM, one VM at a time, and build a
                  report per zone. Latency runs -Rounds rounds with a rotating zone order; throughput runs
                  -Iterations iterations per zone.
      status      Progress of the current or last job.
      wait        Follow the current job until it finishes.
      report      Print the latest summary.
      download    Save the latest report (summary.md/.json, report.html, raw results) under ./results.
      exec        Run any blobperf command on the VM, e.g. -Options 'cleanup'.
      stop        Deallocate the VM(s) (storage accounts and data are kept).
      start       Start the VM(s) again.
      destroy     Delete the resource group and everything in it.

    Azure Managed Lustre (optional; the file system is billed for every hour it exists):
      lustre-deploy    Create the file system (-LustreSku, -LustreCapacityTiB, -LustreZone) and install the
                       Lustre client and lustreperf on every VM (10-20 minutes).
      lustre-info      File system, client and network details from every VM (mounts the file system).
      lustre-run       Load the dataset if needed and run every Lustre test on one VM (lustreperf run-all).
      lustre-zones     Compare availability zones against the file system: latency rounds, then small-file
                       and large-file tests per zone, one VM at a time; downloads lustre-report.html.
      lustre-status, lustre-wait, lustre-report, lustre-download, lustre-exec
                       Like status, wait, report, download and exec, for lustreperf.
      lustre-delete    Unmount the file system on every VM and delete it (stops its billing).

    Commands that talk to one VM use -Vm, or the first VM by name.

.EXAMPLE
    ./scripts/demo.ps1 deploy -Location swedencentral

.EXAMPLE
    ./scripts/demo.ps1 run

.EXAMPLE
    ./scripts/demo.ps1 latency -Options '--reads 2000'

.EXAMPLE
    ./scripts/demo.ps1 deploy -Zones 1,2,3
    ./scripts/demo.ps1 zones

.EXAMPLE
    ./scripts/demo.ps1 lustre-deploy -LustreZone 1
    ./scripts/demo.ps1 lustre-zones
    ./scripts/demo.ps1 lustre-delete
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('deploy', 'info', 'run', 'load', 'latency', 'throughput', 'zones', 'status', 'wait', 'report',
        'download', 'exec', 'stop', 'start', 'destroy', 'lustre-deploy', 'lustre-info', 'lustre-run', 'lustre-zones',
        'lustre-status', 'lustre-wait', 'lustre-report', 'lustre-download', 'lustre-exec', 'lustre-delete')]
    [string] $Command,

    # Resource group that holds (or will hold) the demo.
    [string] $ResourceGroup = 'rg-blobperf-demo',

    # Azure region for 'deploy'.
    [string] $Location = 'swedencentral',

    # VM size for 'deploy' (default: the template default, Standard_D32s_v6).
    [string] $VmSize,

    # 'deploy': availability zones for one VM each, e.g. 1,2,3. Default: keep the zones of existing VMs.
    [string[]] $Zones,

    # SSH public key for 'deploy'. Default: ~/.ssh/id_ed25519.pub or id_rsa.pub, else a throwaway key.
    [string] $SshPublicKeyPath,

    # 'deploy' only: allow SSH from this CIDR, or 'me' for your current public IP. Default: no inbound access.
    [string] $AllowSshFrom,

    # VM to use for single-VM commands (default: the first VM by name).
    [string] $Vm,

    # Extra options for the blobperf command on the VM, e.g. '--iterations 1 --reads 2000'.
    [string] $Options = '',

    # 'zones': latency rounds; each round tests every zone once, in a rotating order.
    [ValidateRange(1, 20)]
    [int] $Rounds = 3,

    # 'zones': throughput iterations per zone ('lustre-zones': small-file and large-file iterations per zone).
    [ValidateRange(1, 20)]
    [int] $Iterations = 3,

    # 'lustre-deploy': Azure Managed Lustre SKU (throughput per TiB).
    [ValidateSet('AMLFS-Durable-Premium-40', 'AMLFS-Durable-Premium-125', 'AMLFS-Durable-Premium-250', 'AMLFS-Durable-Premium-500')]
    [string] $LustreSku = 'AMLFS-Durable-Premium-500',

    # 'lustre-deploy': capacity in TiB, a multiple of the SKU increment (Premium-500: 4).
    [ValidateRange(4, 2048)]
    [int] $LustreCapacityTiB = 4,

    # 'lustre-deploy': availability zone of the file system.
    [ValidateSet('1', '2', '3')]
    [string] $LustreZone = '1',

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
$Lustreperf = '/usr/local/bin/lustreperf'
$script:VmName = $null

function Invoke-AzCli {
    $output = & az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..1] -join ' ') failed (exit code $LASTEXITCODE)." }
    $output
}

# All VMs in the resource group, sorted by name: name, zone ('' when not zonal), id. Empty if there is none.
function Get-BenchmarkVM {
    $json = az vm list --resource-group $ResourceGroup --query '[].{name:name, zone:zones[0], id:id}' --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return @() }
    @(($json -join "`n") | ConvertFrom-Json | Sort-Object name | ForEach-Object {
            [pscustomobject]@{ name = $_.name; zone = "$($_.zone)"; id = $_.id }
        })
}

function Get-VmName {
    if (-not $script:VmName) {
        $script:VmName = if ($Vm) { $Vm } else { (Get-BenchmarkVM | Select-Object -First 1).name }
        if (-not $script:VmName) { throw "No VM in resource group '$ResourceGroup'. Run './scripts/demo.ps1 deploy' first." }
    }
    $script:VmName
}

# Runs a shell script on a VM as root and returns its stdout/stderr (run-command keeps ~4 KB of each).
function Invoke-OnVm([string] $Script, [string] $VmName) {
    if (-not $VmName) { $VmName = Get-VmName }
    $file = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($file.FullName, "#!/bin/bash`n$($Script -replace "`r`n", "`n")`n")
        $json = Invoke-AzCli vm run-command invoke --resource-group $ResourceGroup --name $VmName `
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

function Start-VmJob([string] $ToolCommand, [string] $VmName, [string] $Tool = $Blobperf) {
    $result = Invoke-OnVm "$Tool start $ToolCommand" -VmName $VmName
    Write-VmOutput $result
    if ($result.Stdout -notmatch 'started job') { throw 'The job did not start (see the output above).' }
}

function Wait-VmJob([string] $VmName, [string] $Tool = $Blobperf) {
    $seen = [System.Collections.Generic.HashSet[string]]::new()
    while ($true) {
        $lines = (Invoke-OnVm "$Tool status --lines 12" -VmName $VmName).Stdout -split "`r?`n"
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

function Invoke-VmJob([string] $ToolCommand, [string] $VmName, [switch] $Report, [switch] $Follow, [string] $Tool = $Blobperf) {
    Start-VmJob $ToolCommand -VmName $VmName -Tool $Tool
    if ($NoWait -and -not $Follow) {
        $wait = if ($Tool -eq $Lustreperf) { 'lustre-wait' } else { 'wait' }
        Write-Host "Follow it with: ./scripts/demo.ps1 $wait -ResourceGroup $ResourceGroup"
        return
    }
    $state = Wait-VmJob -VmName $VmName -Tool $Tool
    if ($state -ne 'succeeded') { throw "The job finished with state '$state'. See './scripts/demo.ps1 status' (or lustre-status)." }
    if ($Report) { Write-VmOutput (Invoke-OnVm "$Tool report --print md" -VmName $VmName) }
}

# Downloads the latest report bundle from a VM in checksummed chunks; returns the local folder.
function Save-Report([string] $VmName, [string] $Label = '', [string] $Tool = $Blobperf) {
    $chunks = [System.Collections.Generic.List[string]]::new()
    $total, $sha = 1, ''
    for ($i = 0; $i -lt $total; $i++) {
        $out = (Invoke-OnVm "$Tool export --chunk $i" -VmName $VmName).Stdout
        $header, $body = $out -split "`r?`n", 2
        if ($header -notmatch '(?:BLOB|LUSTRE)PERF-EXPORT chunk=\d+ total=(\d+) sha256=([0-9a-f]{64})') {
            throw "Unexpected export output: $out"
        }
        $total, $sha = [int]$Matches[1], $Matches[2]
        $chunks.Add(($body -replace '\s', ''))
        Write-Host "  downloaded part $($i + 1)/$total"
    }
    $bytes = [Convert]::FromBase64String(-join $chunks)
    $actual = -join ([Security.Cryptography.SHA256]::HashData($bytes) | ForEach-Object { $_.ToString('x2') })
    if ($actual -ne $sha) { throw 'Checksum mismatch in the downloaded report.' }
    $destination = Join-Path $RepoRoot ('results/{0:yyyyMMdd-HHmmss}{1}' -f (Get-Date), ($Label ? "-$Label" : ''))
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $archive = Join-Path $destination 'bundle.tgz'
    [IO.File]::WriteAllBytes($archive, $bytes)
    tar -xzf $archive -C $destination
    if ($LASTEXITCODE -ne 0) { throw 'Could not extract the report bundle.' }
    Remove-Item $archive
    Write-Host "Saved to $destination"
    Get-ChildItem $destination | ForEach-Object { Write-Host "  $($_.Name)" }
    return $destination
}

function Get-ZoneMap {
    $location = Invoke-AzCli group show --name $ResourceGroup --query location --output tsv
    $mappings = (Invoke-AzCli account list-locations --output json | ConvertFrom-Json | Where-Object name -eq $location).availabilityZoneMappings
    (@($mappings) | Where-Object { $_ } | ForEach-Object { "$($_.logicalZone)=$($_.physicalZone)" }) -join ','
}

function Invoke-ZoneComparison {
    $vms = @(Get-BenchmarkVM | Where-Object zone | Sort-Object zone)
    if ($vms.Count -lt 2) { throw "Comparing zones needs one VM per zone: ./scripts/demo.ps1 deploy -Zones 1,2,3" }
    Write-Host "Starting $($vms.Count) VMs: $($vms.name -join ', ')"
    Invoke-AzCli vm start --ids @($vms.id) | Out-Null
    $zoneMap = Get-ZoneMap
    if ($zoneMap) { Write-Host "Logical to physical zones for this subscription: $zoneMap" }
    $since = (Get-Date).ToUniversalTime().AddSeconds(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
    # One job at a time: rotating the order each round keeps drift over time from looking like a zone effect.
    for ($round = 0; $round -lt $Rounds; $round++) {
        for ($k = 0; $k -lt $vms.Count; $k++) {
            $target = $vms[($round + $k) % $vms.Count]
            Write-Host "`n=== latency round $($round + 1)/${Rounds}: zone $($target.zone) ($($target.name)) ==="
            Invoke-VmJob 'latency' -VmName $target.name -Follow
        }
    }
    foreach ($target in $vms) {
        Write-Host "`n=== throughput: zone $($target.zone) ($($target.name)) ==="
        Invoke-VmJob "throughput --iterations $Iterations" -VmName $target.name -Follow
    }
    Write-Host "`n=== zone report ==="
    $report = "$Blobperf report --zones --since $since --print none" + ($zoneMap ? " --zone-map $zoneMap" : '')
    Write-VmOutput (Invoke-OnVm $report -VmName $vms[0].name)
    $destination = Save-Report -VmName $vms[0].name -Label 'zones'
    Get-Content (Join-Path $destination 'summary.md') | Write-Host
}

# --- Azure Managed Lustre (infra/lustre.bicep, vm/lustreperf.py) ---

function Get-LustreFileSystem {
    $json = az resource list --resource-group $ResourceGroup --resource-type Microsoft.StorageCache/amlFilesystems `
        --query '[].{name:name, id:id, zone:zones[0]}' --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return @() }
    @(($json -join "`n") | ConvertFrom-Json)
}

# The setup can reboot a VM into a kernel that has the Lustre client module; wait until it runs one.
function Wait-LustreClient([string] $VmName) {
    $check = 'if modinfo lustre >/dev/null 2>&1; then echo "CLIENT-OK $(uname -r)"; else echo "CLIENT-MISSING $(uname -r)"; fi'
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        $out = try { (Invoke-OnVm $check -VmName $VmName).Stdout } catch { "$_" }
        if ($out -match 'CLIENT-OK (\S+)') {
            Write-Host "${VmName}: Lustre client ready (kernel $($Matches[1]))"
            return
        }
        Start-Sleep -Seconds 30
    }
    throw "$VmName has no Lustre client module for its running kernel; run './scripts/demo.ps1 lustre-deploy' again."
}

function Invoke-LustreDeploy {
    $vms = @(Get-BenchmarkVM)
    if (-not $vms) { throw "No VM in resource group '$ResourceGroup'. Run './scripts/demo.ps1 deploy' first." }
    $subnet = az network vnet subnet show --resource-group $ResourceGroup --vnet-name vnet-blobperf --name snet-lustre `
        --query id --output tsv 2>$null
    if (-not $subnet) { throw "The VNet has no snet-lustre subnet yet: run './scripts/demo.ps1 deploy' first, which adds it." }
    Write-Host 'Registering the Microsoft.StorageCache resource provider (once per subscription)...'
    Invoke-AzCli provider register --namespace Microsoft.StorageCache --wait | Out-Null
    Write-Host "Starting $($vms.Count) VM(s): the Lustre client setup runs on each of them"
    Invoke-AzCli vm start --ids @($vms.id) | Out-Null
    $parameters = [ordered]@{
        location           = @{ value = (Invoke-AzCli group show --name $ResourceGroup --query location --output tsv) }
        vmNames            = @{ value = @($vms.name) }
        skuName            = @{ value = $LustreSku }
        storageCapacityTiB = @{ value = $LustreCapacityTiB }
        zone               = @{ value = $LustreZone }
    }
    $file = New-TemporaryFile
    try {
        @{
            '$schema'      = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
            contentVersion = '1.0.0.0'
            parameters     = $parameters
        } | ConvertTo-Json -Depth 5 | Set-Content -Path $file.FullName -Encoding utf8NoBOM
        Write-Host "Creating $LustreSku, $LustreCapacityTiB TiB, in zone $LustreZone and setting up the clients (10-20 minutes)."
        Write-Warning "The file system is billed for every hour it exists, from now until './scripts/demo.ps1 lustre-delete'."
        $outputs = Invoke-AzCli deployment group create --resource-group $ResourceGroup --name "lustre-$ResourceGroup" `
            --template-file (Join-Path $RepoRoot 'infra/lustre.bicep') --parameters "@$($file.FullName)" `
            --query properties.outputs --output json | ConvertFrom-Json
    }
    finally {
        Remove-Item $file.FullName -Force -ErrorAction SilentlyContinue
    }
    Write-Host "File system: $($outputs.fileSystemName.value), zone $($outputs.zone.value), Lustre $($outputs.lustreVersion.value), MGS $($outputs.mgsAddress.value)"
    foreach ($target in $vms) { Wait-LustreClient -VmName $target.name }
    foreach ($target in $vms) {
        Write-Host "== $($target.name)$($target.zone ? " (zone $($target.zone))" : '') =="
        Write-VmOutput (Invoke-OnVm "$Lustreperf info" -VmName $target.name)
    }
    $next = if (@($vms | Where-Object zone).Count -ge 2) { 'lustre-zones' } else { 'lustre-run' }
    Write-Host "`nNext: ./scripts/demo.ps1 $next, then ./scripts/demo.ps1 lustre-delete"
}

function Invoke-LustreZoneComparison {
    $vms = @(Get-BenchmarkVM | Where-Object zone | Sort-Object zone)
    if ($vms.Count -lt 2) { throw "Comparing zones needs one VM per zone: ./scripts/demo.ps1 deploy -Zones 1,2,3" }
    $fs = @(Get-LustreFileSystem)
    if (-not $fs) { throw "No Azure Managed Lustre file system in '$ResourceGroup': run './scripts/demo.ps1 lustre-deploy' first." }
    Write-Host "Starting $($vms.Count) VMs: $($vms.name -join ', ')"
    Invoke-AzCli vm start --ids @($vms.id) | Out-Null
    $zoneMap = Get-ZoneMap
    if ($zoneMap) { Write-Host "Logical to physical zones for this subscription: $zoneMap" }
    Write-Host "File system $($fs[0].name) is in zone $($fs[0].zone)"
    $since = (Get-Date).ToUniversalTime().AddSeconds(-5).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $loader = @($vms | Where-Object zone -eq $fs[0].zone)[0] ?? $vms[0]
    Write-Host "`n=== latency dataset: $($loader.name) ==="
    Invoke-VmJob 'load' -VmName $loader.name -Follow -Tool $Lustreperf
    # One job at a time: rotating the order each round keeps drift over time from looking like a zone effect.
    for ($round = 0; $round -lt $Rounds; $round++) {
        for ($k = 0; $k -lt $vms.Count; $k++) {
            $target = $vms[($round + $k) % $vms.Count]
            Write-Host "`n=== latency round $($round + 1)/${Rounds}: zone $($target.zone) ($($target.name)) ==="
            Invoke-VmJob 'latency' -VmName $target.name -Follow -Tool $Lustreperf
        }
    }
    foreach ($test in 'throughput', 'bandwidth') {
        foreach ($target in $vms) {
            Write-Host "`n=== ${test}: zone $($target.zone) ($($target.name)) ==="
            Invoke-VmJob "$test --iterations $Iterations" -VmName $target.name -Follow -Tool $Lustreperf
        }
    }
    Write-Host "`n=== zone report ==="
    $report = "$Lustreperf report --zones --since $since --print none" + ($zoneMap ? " --zone-map $zoneMap" : '')
    Write-VmOutput (Invoke-OnVm $report -VmName $vms[0].name)
    $destination = Save-Report -VmName $vms[0].name -Label 'lustre-zones' -Tool $Lustreperf
    Get-Content (Join-Path $destination 'lustre-summary.md') | Write-Host
    Write-Warning "The file system still exists and is billed by the hour: './scripts/demo.ps1 lustre-delete' deletes it."
}

function Invoke-LustreDelete {
    $fs = @(Get-LustreFileSystem)
    if (-not $fs) {
        Write-Host "No Azure Managed Lustre file system in resource group '$ResourceGroup'."
        return
    }
    if (-not $Yes) {
        $answer = Read-Host "Delete Azure Managed Lustre file system $($fs.name -join ', ') and all data on it? (yes/no)"
        if ($answer -ne 'yes') { Write-Host 'Cancelled.'; return }
    }
    # Unmount first, so that no client keeps retrying servers that are gone. Deallocated VMs have nothing mounted.
    foreach ($target in Get-BenchmarkVM) {
        $power = az vm get-instance-view --ids $target.id --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" `
            --output tsv 2>$null
        if ($power -eq 'PowerState/running') {
            Write-Host "== $($target.name) =="
            try { Write-VmOutput (Invoke-OnVm "$Lustreperf umount" -VmName $target.name) } catch { Write-Warning "$($target.name): $_" }
        }
    }
    Write-Host "Deleting $($fs.name -join ', ') (5-15 minutes)..."
    Invoke-AzCli resource delete --ids @($fs.id) | Out-Null
    $left = @(Get-LustreFileSystem)
    if ($left) { throw "Still present: $($left.name -join ', '). Delete it in the Azure portal." }
    Write-Host 'Deleted: the file system no longer exists and is no longer billed.'
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
        if ($SshPublicKeyPath) { Write-Warning 'The VMs already exist and keep their current SSH key (change it with az vm user update).' }
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
    Write-Host 'No SSH key found: using a throwaway key (the VMs are managed with run-command only).'
    New-ThrowawaySshKey
}

function Invoke-Deploy {
    $parameters = [ordered]@{
        location          = @{ value = $Location }
        resourceGroupName = @{ value = $ResourceGroup }
        adminSshPublicKey = @{ value = (Get-SshPublicKey) }
    }
    if ($VmSize) { $parameters.vmSize = @{ value = $VmSize } }
    # Incremental deployments never delete VMs, so keep the zones of existing zonal VMs unless told otherwise.
    [string[]] $zoneList = if ($Zones) { $Zones -split '[,\s]+' | Where-Object { $_ } } else { Get-BenchmarkVM | Where-Object zone | ForEach-Object zone | Sort-Object -Unique }
    if ($zoneList) {
        Write-Host "One VM in each of zones $($zoneList -join ', ')"
        $parameters.vmZones = @{ value = $zoneList }
    }
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
    Write-Host "VMs:               $($outputs.vmNames.value -join ', ')"
    Write-Host "Premium account:   $($outputs.premiumAccountName.value)"
    Write-Host "Standard account:  $($outputs.standardAccountName.value)"
    $ips = @($outputs.vmPublicIpAddresses.value | Where-Object { $_ })
    if ($ips) { Write-Host "SSH:               $(($ips | ForEach-Object { "azureuser@$_" }) -join ', ')" }
    Write-Host ''
    $next = if ($zoneList) { './scripts/demo.ps1 zones    (runs the tests in each zone, one VM at a time)' } else { './scripts/demo.ps1 run    (loads the dataset on first use, then runs every test)' }
    Write-Host "Next: $next"
}

function Get-TargetVM {
    $vms = @(Get-BenchmarkVM)
    if ($Vm) { $vms = @($vms | Where-Object name -eq $Vm) }
    if (-not $vms) { throw "No VM found in resource group '$ResourceGroup'." }
    $vms
}

switch ($Command) {
    'deploy' { Invoke-Deploy }
    'info' {
        foreach ($target in Get-TargetVM) {
            Write-Host "== $($target.name)$($target.zone ? " (zone $($target.zone))" : '') =="
            Write-VmOutput (Invoke-OnVm "$Blobperf info" -VmName $target.name)
        }
    }
    'run' { Invoke-VmJob "run-all $Options" -Report }
    'load' { Invoke-VmJob "load $Options" }
    'latency' { Invoke-VmJob "latency $Options" -Report }
    'throughput' { Invoke-VmJob "throughput $Options" -Report }
    'zones' { Invoke-ZoneComparison }
    'status' { Write-VmOutput (Invoke-OnVm "$Blobperf status --lines 25") }
    'wait' {
        $state = Wait-VmJob
        if ($state -ne 'succeeded') { exit 1 }
    }
    'report' { Write-VmOutput (Invoke-OnVm "$Blobperf report --print md") }
    'download' { Save-Report | Out-Null }
    'exec' {
        if (-not $Options) { throw "Pass the blobperf command with -Options, e.g. -Options 'cleanup'." }
        Write-VmOutput (Invoke-OnVm "$Blobperf $Options")
    }
    'stop' {
        $targets = Get-TargetVM
        Invoke-AzCli vm deallocate --ids @($targets.id) | Out-Null
        Write-Host "Deallocated: $($targets.name -join ', ')"
    }
    'start' {
        $targets = Get-TargetVM
        Invoke-AzCli vm start --ids @($targets.id) | Out-Null
        Write-Host "Started: $($targets.name -join ', ')"
    }
    'destroy' {
        if (-not $Yes) {
            $answer = Read-Host "Delete resource group '$ResourceGroup' and everything in it? (yes/no)"
            if ($answer -ne 'yes') { Write-Host 'Cancelled.'; return }
        }
        Invoke-AzCli group delete --name $ResourceGroup --yes --no-wait | Out-Null
        Write-Host "Deleting resource group '$ResourceGroup' in the background."
    }
    'lustre-deploy' { Invoke-LustreDeploy }
    'lustre-info' {
        foreach ($target in Get-TargetVM) {
            Write-Host "== $($target.name)$($target.zone ? " (zone $($target.zone))" : '') =="
            Write-VmOutput (Invoke-OnVm "$Lustreperf info" -VmName $target.name)
        }
    }
    'lustre-run' { Invoke-VmJob "run-all $Options" -Report -Tool $Lustreperf }
    'lustre-zones' { Invoke-LustreZoneComparison }
    'lustre-status' { Write-VmOutput (Invoke-OnVm "$Lustreperf status --lines 25") }
    'lustre-wait' {
        $state = Wait-VmJob -Tool $Lustreperf
        if ($state -ne 'succeeded') { exit 1 }
    }
    'lustre-report' { Write-VmOutput (Invoke-OnVm "$Lustreperf report --print md") }
    'lustre-download' { Save-Report -Label 'lustre' -Tool $Lustreperf | Out-Null }
    'lustre-exec' {
        if (-not $Options) { throw "Pass the lustreperf command with -Options, e.g. -Options 'cleanup'." }
        Write-VmOutput (Invoke-OnVm "$Lustreperf $Options")
    }
    'lustre-delete' { Invoke-LustreDelete }
}
