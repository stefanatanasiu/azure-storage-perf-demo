#!/usr/bin/env bash
# Deploy and run the Azure Premium vs Standard block blob performance demo (bash + Azure CLI).
# Same commands as scripts/demo.ps1. Run './scripts/demo.sh help' for usage.
set -euo pipefail

RESOURCE_GROUP=${RESOURCE_GROUP:-rg-blobperf-demo}
LOCATION=${LOCATION:-swedencentral}
VM_SIZE=${VM_SIZE:-}
ZONES=${ZONES:-}
SSH_PUBLIC_KEY_PATH=${SSH_PUBLIC_KEY_PATH:-}
ALLOW_SSH_FROM=${ALLOW_SSH_FROM:-}
VM_OPT=${VM:-}
OPTIONS=${OPTIONS:-}
ROUNDS=${ROUNDS:-3}
ITERATIONS=${ITERATIONS:-3}
POLL_SECONDS=${POLL_SECONDS:-60}
LUSTRE_SKU=${LUSTRE_SKU:-AMLFS-Durable-Premium-500}
LUSTRE_CAPACITY_TIB=${LUSTRE_CAPACITY_TIB:-4}
LUSTRE_ZONE=${LUSTRE_ZONE:-1}
NO_WAIT=false
YES=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLOBPERF=/usr/local/bin/blobperf
LUSTREPERF=/usr/local/bin/lustreperf
# The VM-side tool that start_job, wait_job, run_job and download use; the lustre-* commands switch it.
TOOL=$BLOBPERF
VM_NAME=""
DOWNLOADED_TO=""

usage() {
  cat <<'EOF'
Usage: ./scripts/demo.sh <command> [options]

Commands:
  deploy      Deploy or update the resource group, network, storage accounts and VM(s).
              --zones 1,2,3 deploys one VM per availability zone (see zones).
  info        VM details and access to both storage accounts (every VM).
  run         Load the dataset if needed, run every test and print the summary (blobperf run-all).
  load        Load the 1,000,000 x 64 KiB latency dataset only.
  latency     Latency test only.
  throughput  AzCopy upload/download and list test only.
  zones       Compare availability zones: run the tests on each zonal VM, one VM at a time, and build a
              report per zone (latency: --rounds rounds in a rotating zone order; throughput: --iterations).
  status      Progress of the current or last job.
  wait        Follow the current job until it finishes.
  report      Print the latest summary.
  download    Save the latest report (summary.md/.json, report.html, raw results) under ./results.
  exec        Run any blobperf command on the VM, e.g. --options cleanup
  stop        Deallocate the VM(s) (storage accounts and data are kept).
  start       Start the VM(s) again.
  destroy     Delete the resource group and everything in it.

Azure Managed Lustre (optional; the file system is billed for every hour it exists):
  lustre-deploy    Create the file system (--lustre-sku, --lustre-capacity, --lustre-zone) and install the
                   Lustre client and lustreperf on every VM (10-20 minutes).
  lustre-info      File system, client and network details from every VM (mounts the file system).
  lustre-run       Load the dataset if needed and run every Lustre test on one VM (lustreperf run-all).
  lustre-zones     Compare availability zones against the file system: latency rounds, then small-file and
                   large-file tests per zone, one VM at a time; downloads lustre-report.html.
  lustre-status, lustre-wait, lustre-report, lustre-download, lustre-exec
                   Like status, wait, report, download and exec, for lustreperf.
  lustre-delete    Unmount the file system on every VM and delete it (stops its billing).

Options (or the environment variables in brackets):
  -g, --resource-group NAME   resource group (RESOURCE_GROUP, default rg-blobperf-demo)
  -l, --location REGION       region for deploy (LOCATION, default swedencentral)
  --vm-size SIZE              VM size for deploy (VM_SIZE, default Standard_D32s_v6)
  --zones 1,2,3               deploy one VM per zone (ZONES, default: keep the zones of existing VMs)
  --ssh-key PATH              SSH public key for deploy (SSH_PUBLIC_KEY_PATH)
  --allow-ssh-from CIDR|me    open SSH to this source on deploy (ALLOW_SSH_FROM, default: no inbound access)
  --vm NAME                   VM for single-VM commands (VM, default: the first VM by name)
  --options "..."             extra blobperf options, e.g. "--iterations 1" (OPTIONS)
  --rounds N                  zones: latency rounds (ROUNDS, default 3)
  --iterations N              zones: throughput iterations per zone (ITERATIONS, default 3)
  --lustre-sku SKU            lustre-deploy: AMLFS SKU (LUSTRE_SKU, default AMLFS-Durable-Premium-500)
  --lustre-capacity TIB       lustre-deploy: capacity in TiB (LUSTRE_CAPACITY_TIB, default 4)
  --lustre-zone 1|2|3         lustre-deploy: availability zone of the file system (LUSTRE_ZONE, default 1)
  --poll SECONDS              seconds between progress checks (POLL_SECONDS, default 60)
  --no-wait                   start the job and return
  --yes                       do not ask for confirmation in destroy
EOF
}

die() { echo "error: $*" >&2; exit 1; }

# One line per VM, sorted by name: name<TAB>zone<TAB>id (zone empty when the VM is not zonal).
list_vms() {
  az vm list --resource-group "$RESOURCE_GROUP" --query "sort_by([], &name)[].[name, zones[0] || '', id]" \
    --output tsv 2>/dev/null || true
}

vm_name() {
  local name=$VM_OPT
  [[ -n "$name" ]] || name=$(list_vms | head -n 1 | cut -f1)
  [[ -n "$name" ]] || die "no VM in resource group '$RESOURCE_GROUP'; run './scripts/demo.sh deploy' first"
  echo "$name"
}

# Runs a shell script on VM_NAME as root and prints its stdout (run-command keeps ~4 KB of output).
on_vm() {
  local script message
  script=$(mktemp)
  printf '#!/bin/bash\n%s\n' "$1" > "$script"
  message=$(az vm run-command invoke --resource-group "$RESOURCE_GROUP" --name "$VM_NAME" \
    --command-id RunShellScript --scripts @"$script" --query 'value[0].message' --output tsv < /dev/null)
  rm -f "$script"
  printf '%s\n' "$message" | awk '/^\[stdout\]/{out=1; next} /^\[stderr\]/{out=0; err=1; next} out{print} err && NF{print > "/dev/stderr"}'
}

start_job() {
  local output
  output=$(on_vm "$TOOL start $1")
  echo "$output"
  [[ "$output" == *"started job"* ]] || die "the job did not start (see the output above)"
}

wait_job() {
  local seen output state
  seen=$(mktemp)
  while true; do
    output=$(on_vm "$TOOL status --lines 12")
    state=$(head -n 1 <<<"$output" | sed -n 's/.*STATE: \([a-z]*\).*/\1/p')
    tail -n +2 <<<"$output" | while IFS= read -r line; do
      if [[ -n "${line// /}" ]] && ! grep -Fxq -- "$line" "$seen"; then
        echo "$line"
        echo "$line" >> "$seen"
      fi
    done
    if [[ "$state" != "running" ]]; then
      head -n 1 <<<"$output"
      rm -f "$seen"
      [[ "$state" == "succeeded" ]]
      return
    fi
    sleep "$POLL_SECONDS"
  done
}

run_job() {
  start_job "$1"
  if [[ "$NO_WAIT" == true ]]; then
    echo "Follow it with: ./scripts/demo.sh $([[ "$TOOL" == "$LUSTREPERF" ]] && echo lustre-wait || echo wait) -g $RESOURCE_GROUP"
    return
  fi
  wait_job || die "the job did not succeed; see './scripts/demo.sh status' (or lustre-status)"
  if [[ "${2:-}" == report ]]; then on_vm "$TOOL report --print md"; fi
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# GNU coreutils, BSD/macOS and OpenSSL spell "decode" differently; an unknown option fails before any output.
b64decode() { base64 --decode < "$1" 2>/dev/null || base64 -D < "$1" 2>/dev/null || openssl base64 -d -A < "$1"; }

# Downloads the latest report bundle from VM_NAME in checksummed chunks; sets DOWNLOADED_TO.
download() {
  local i=0 total=1 sha="" output header destination encoded
  destination="$REPO_ROOT/results/$(date +%Y%m%d-%H%M%S)${1:+-$1}"
  mkdir -p "$destination"
  encoded="$destination/bundle.b64"
  : > "$encoded"
  while (( i < total )); do
    output=$(on_vm "$TOOL export --chunk $i")
    header=$(head -n 1 <<<"$output")
    [[ "$header" =~ (BLOB|LUSTRE)PERF-EXPORT\ chunk=[0-9]+\ total=([0-9]+)\ sha256=([0-9a-f]{64}) ]] || die "unexpected export output: $output"
    total=${BASH_REMATCH[2]}
    sha=${BASH_REMATCH[3]}
    tail -n +2 <<<"$output" | tr -d '[:space:]' >> "$encoded"
    i=$((i + 1))
    echo "  downloaded part $i/$total"
  done
  b64decode "$encoded" > "$destination/bundle.tgz"
  [[ "$(sha256 "$destination/bundle.tgz")" == "$sha" ]] || die "checksum mismatch in the downloaded report"
  tar -xzf "$destination/bundle.tgz" -C "$destination"
  rm -f "$encoded" "$destination/bundle.tgz"
  echo "Saved to $destination"
  for file in "$destination"/*; do echo "  ${file##*/}"; done
  DOWNLOADED_TO=$destination
}

# "1=swedencentral-az2,2=..." for the resource group's region in this subscription (empty without zones).
get_zone_map() {
  local location
  location=$(az group show --name "$RESOURCE_GROUP" --query location --output tsv)
  az account list-locations --query "[?name=='$location'].availabilityZoneMappings[].[logicalZone, physicalZone]" \
    --output tsv | awk -F'\t' '{printf "%s%s=%s", (NR > 1 ? "," : ""), $1, $2}'
}

compare_zones() {
  local names=() zone_ids=() ids=() name zone id count round k i zone_map since
  while IFS=$'\t' read -r name zone id; do
    [[ -n "$zone" ]] || continue
    names+=("$name"); zone_ids+=("$zone"); ids+=("$id")
  done < <(list_vms | sort -t $'\t' -k2,2)
  count=${#names[@]}
  (( count >= 2 )) || die "comparing zones needs one VM per zone: ./scripts/demo.sh deploy --zones 1,2,3"
  echo "Starting $count VMs: ${names[*]}"
  MSYS_NO_PATHCONV=1 az vm start --ids "${ids[@]}" --output none
  zone_map=$(get_zone_map)
  [[ -z "$zone_map" ]] || echo "Logical to physical zones for this subscription: $zone_map"
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # One job at a time: rotating the order each round keeps drift over time from looking like a zone effect.
  for (( round = 0; round < ROUNDS; round++ )); do
    for (( k = 0; k < count; k++ )); do
      i=$(( (round + k) % count ))
      VM_NAME=${names[$i]}
      printf '\n=== latency round %d/%d: zone %s (%s) ===\n' $((round + 1)) "$ROUNDS" "${zone_ids[$i]}" "$VM_NAME"
      start_job latency
      wait_job || die "the latency job on $VM_NAME did not succeed"
    done
  done
  for (( i = 0; i < count; i++ )); do
    VM_NAME=${names[$i]}
    printf '\n=== throughput: zone %s (%s) ===\n' "${zone_ids[$i]}" "$VM_NAME"
    start_job "throughput --iterations $ITERATIONS"
    wait_job || die "the throughput job on $VM_NAME did not succeed"
  done
  printf '\n=== zone report ===\n'
  VM_NAME=${names[0]}
  on_vm "$BLOBPERF report --zones --since $since --print none${zone_map:+ --zone-map $zone_map}"
  download zones
  cat "$DOWNLOADED_TO/summary.md"
}

# --- Azure Managed Lustre (infra/lustre.bicep, vm/lustreperf.py) ---

# File systems in the resource group: name<TAB>zone<TAB>id per line.
lustre_fs() {
  az resource list --resource-group "$RESOURCE_GROUP" --resource-type Microsoft.StorageCache/amlFilesystems \
    --query "[].[name, zones[0] || '', id]" --output tsv 2>/dev/null || true
}

# The setup can reboot a VM into a kernel that has the Lustre client module; wait until VM_NAME runs one.
wait_lustre_client() {
  local attempt output
  for (( attempt = 1; attempt <= 20; attempt++ )); do
    # shellcheck disable=SC2016 # expanded on the VM
    output=$(on_vm 'if modinfo lustre >/dev/null 2>&1; then echo "CLIENT-OK $(uname -r)"; else echo "CLIENT-MISSING $(uname -r)"; fi' 2>/dev/null || true)
    if [[ "$output" =~ CLIENT-OK\ ([^[:space:]]+) ]]; then
      echo "$VM_NAME: Lustre client ready (kernel ${BASH_REMATCH[1]})"
      return
    fi
    sleep 30
  done
  die "$VM_NAME has no Lustre client module for its running kernel; run './scripts/demo.sh lustre-deploy' again"
}

lustre_deploy() {
  local names=() ids=() name zone id joined names_json
  while IFS=$'\t' read -r name zone id; do names+=("$name"); ids+=("$id"); done < <(list_vms)
  (( ${#names[@]} )) || die "no VM in resource group '$RESOURCE_GROUP'; run './scripts/demo.sh deploy' first"
  az network vnet subnet show --resource-group "$RESOURCE_GROUP" --vnet-name vnet-blobperf --name snet-lustre \
    --query id --output tsv >/dev/null 2>&1 \
    || die "the VNet has no snet-lustre subnet yet: run './scripts/demo.sh deploy' first, which adds it"
  echo "Registering the Microsoft.StorageCache resource provider (once per subscription)..."
  az provider register --namespace Microsoft.StorageCache --wait --output none
  echo "Starting ${#names[@]} VM(s): the Lustre client setup runs on each of them"
  MSYS_NO_PATHCONV=1 az vm start --ids "${ids[@]}" --output none
  joined=$(IFS=,; echo "${names[*]}")
  names_json="[\"${joined//,/\",\"}\"]"
  echo "Creating $LUSTRE_SKU, $LUSTRE_CAPACITY_TIB TiB, in zone $LUSTRE_ZONE and setting up the clients (10-20 minutes)."
  echo "warning: the file system is billed for every hour it exists, from now until './scripts/demo.sh lustre-delete'." >&2
  az deployment group create --resource-group "$RESOURCE_GROUP" --name "lustre-$RESOURCE_GROUP" \
    --template-file "$REPO_ROOT/infra/lustre.bicep" \
    --parameters vmNames="$names_json" skuName="$LUSTRE_SKU" storageCapacityTiB="$LUSTRE_CAPACITY_TIB" zone="$LUSTRE_ZONE" \
    --query "{fileSystem:properties.outputs.fileSystemName.value, zone:properties.outputs.zone.value, lustre:properties.outputs.lustreVersion.value, mgs:properties.outputs.mgsAddress.value}" \
    --output table
  for VM_NAME in "${names[@]}"; do wait_lustre_client; done
  while IFS=$'\t' read -r VM_NAME zone _; do
    echo "== $VM_NAME${zone:+ (zone $zone)} =="
    on_vm "$LUSTREPERF info"
  done < <(list_vms)
  echo
  echo "Next: ./scripts/demo.sh lustre-zones (or lustre-run), then ./scripts/demo.sh lustre-delete"
}

lustre_zones() {
  local names=() zone_ids=() ids=() name zone id count round k i zone_map since fs fs_zone loader test
  while IFS=$'\t' read -r name zone id; do
    [[ -n "$zone" ]] || continue
    names+=("$name"); zone_ids+=("$zone"); ids+=("$id")
  done < <(list_vms | sort -t $'\t' -k2,2)
  count=${#names[@]}
  (( count >= 2 )) || die "comparing zones needs one VM per zone: ./scripts/demo.sh deploy --zones 1,2,3"
  fs=$(lustre_fs)
  [[ -n "$fs" ]] || die "no Azure Managed Lustre file system in '$RESOURCE_GROUP': run './scripts/demo.sh lustre-deploy' first"
  fs_zone=$(head -n 1 <<<"$fs" | cut -f2 | tr -d '\r')
  echo "Starting $count VMs: ${names[*]}"
  MSYS_NO_PATHCONV=1 az vm start --ids "${ids[@]}" --output none
  zone_map=$(get_zone_map)
  [[ -z "$zone_map" ]] || echo "Logical to physical zones for this subscription: $zone_map"
  echo "File system $(head -n 1 <<<"$fs" | cut -f1) is in zone $fs_zone"
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  TOOL=$LUSTREPERF
  loader=${names[0]}
  for (( i = 0; i < count; i++ )); do
    if [[ "${zone_ids[$i]}" == "$fs_zone" ]]; then loader=${names[$i]}; fi
  done
  VM_NAME=$loader
  printf '\n=== latency dataset: %s ===\n' "$VM_NAME"
  start_job load
  wait_job || die "loading the dataset on $VM_NAME did not succeed"
  # One job at a time: rotating the order each round keeps drift over time from looking like a zone effect.
  for (( round = 0; round < ROUNDS; round++ )); do
    for (( k = 0; k < count; k++ )); do
      i=$(( (round + k) % count ))
      VM_NAME=${names[$i]}
      printf '\n=== latency round %d/%d: zone %s (%s) ===\n' $((round + 1)) "$ROUNDS" "${zone_ids[$i]}" "$VM_NAME"
      start_job latency
      wait_job || die "the latency job on $VM_NAME did not succeed"
    done
  done
  for test in throughput bandwidth; do
    for (( i = 0; i < count; i++ )); do
      VM_NAME=${names[$i]}
      printf '\n=== %s: zone %s (%s) ===\n' "$test" "${zone_ids[$i]}" "$VM_NAME"
      start_job "$test --iterations $ITERATIONS"
      wait_job || die "the $test job on $VM_NAME did not succeed"
    done
  done
  printf '\n=== zone report ===\n'
  VM_NAME=${names[0]}
  on_vm "$LUSTREPERF report --zones --since $since --print none${zone_map:+ --zone-map $zone_map}"
  download lustre-zones
  cat "$DOWNLOADED_TO/lustre-summary.md"
  echo "warning: the file system still exists and is billed by the hour: './scripts/demo.sh lustre-delete' deletes it." >&2
}

lustre_delete() {
  local fs power vm_id answer
  fs=$(lustre_fs | tr -d '\r')
  if [[ -z "$fs" ]]; then
    echo "No Azure Managed Lustre file system in resource group '$RESOURCE_GROUP'."
    return
  fi
  if [[ "$YES" != true ]]; then
    read -r -p "Delete Azure Managed Lustre file system $(cut -f1 <<<"$fs" | paste -sd, -) and all data on it? (yes/no) " answer
    [[ "$answer" == yes ]] || { echo "Cancelled."; return; }
  fi
  # Unmount first, so that no client keeps retrying servers that are gone. Deallocated VMs have nothing mounted.
  while IFS=$'\t' read -r VM_NAME _ vm_id; do
    power=$(MSYS_NO_PATHCONV=1 az vm get-instance-view --ids "$vm_id" \
      --query "instanceView.statuses[?starts_with(code, 'PowerState/')].code | [0]" --output tsv 2>/dev/null || true)
    if [[ "$power" == PowerState/running* ]]; then
      echo "== $VM_NAME =="
      on_vm "$LUSTREPERF umount" || echo "warning: could not unmount on $VM_NAME" >&2
    fi
  done < <(list_vms)
  echo "Deleting $(cut -f1 <<<"$fs" | paste -sd, -) (5-15 minutes)..."
  # shellcheck disable=SC2046 # one argument per resource ID
  MSYS_NO_PATHCONV=1 az resource delete --ids $(cut -f3 <<<"$fs") --output none
  [[ -z "$(lustre_fs)" ]] || die "the file system still exists; delete it in the Azure portal"
  echo "Deleted: the file system no longer exists and is no longer billed."
}

ssh_public_key() {
  local candidate tmp existing
  # Azure cannot change the key of an existing VM by redeploying, so keep whatever it already has.
  existing=$(az vm list --resource-group "$RESOURCE_GROUP" \
    --query '[0].osProfile.linuxConfiguration.ssh.publicKeys[0].keyData' --output tsv 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    [[ -z "$SSH_PUBLIC_KEY_PATH" ]] || echo "warning: the VMs already exist and keep their current SSH key (change it with 'az vm user update')." >&2
    echo "Reusing the SSH key of the existing VM." >&2
    echo "$existing"
    return
  fi
  if [[ -n "$SSH_PUBLIC_KEY_PATH" ]]; then cat "$SSH_PUBLIC_KEY_PATH"; return; fi
  for candidate in ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub ~/.ssh/id_ecdsa.pub; do
    if [[ -f "$candidate" ]]; then echo "Using SSH public key $candidate" >&2; cat "$candidate"; return; fi
  done
  [[ -z "$ALLOW_SSH_FROM" ]] || die "SSH access requested but no public key found; pass --ssh-key"
  # Azure needs an admin credential for a Linux VM; when SSH is not used, a key nobody holds is the safest one.
  echo "No SSH key found: using a throwaway key (the VMs are managed with run-command only)." >&2
  tmp=$(mktemp -d)
  ssh-keygen -q -t ed25519 -N '' -C blobperf-throwaway -f "$tmp/key"
  cat "$tmp/key.pub"
  rm -rf "$tmp"
}

deploy() {
  local key cidr params zone_list
  key=$(ssh_public_key)
  params=(location="$LOCATION" resourceGroupName="$RESOURCE_GROUP" adminSshPublicKey="$key")
  [[ -z "$VM_SIZE" ]] || params+=(vmSize="$VM_SIZE")
  # Incremental deployments never delete VMs, so keep the zones of existing zonal VMs unless told otherwise.
  zone_list=$ZONES
  [[ -n "$zone_list" ]] || zone_list=$(list_vms | awk -F'\t' '$2 != "" {print $2}' | sort -u | paste -sd, -)
  if [[ -n "$zone_list" ]]; then
    echo "One VM in each of zones $zone_list"
    params+=(vmZones="[\"$(sed -E 's/[[:space:]]*,[[:space:]]*/","/g' <<<"$zone_list")\"]")
  fi
  if [[ -n "$ALLOW_SSH_FROM" ]]; then
    cidr=$ALLOW_SSH_FROM
    [[ "$cidr" != me ]] || cidr="$(curl -fsS https://api.ipify.org)/32"
    echo "SSH will be allowed from $cidr"
    params+=(sshSourceAddressPrefix="$cidr")
  fi
  echo "Deploying to subscription '$(az account show --query name --output tsv)', region $LOCATION, resource group $RESOURCE_GROUP."
  echo "This takes about 5-10 minutes..."
  az deployment sub create --name "blobperf-$RESOURCE_GROUP" --location "$LOCATION" \
    --template-file "$REPO_ROOT/infra/main.bicep" --parameters "${params[@]}" \
    --query "{vms:join(', ', properties.outputs.vmNames.value), premium:properties.outputs.premiumAccountName.value, standard:properties.outputs.standardAccountName.value}" \
    --output table
  echo
  if [[ -n "$zone_list" ]]; then
    echo "Next: ./scripts/demo.sh zones    (runs the tests in each zone, one VM at a time)"
  else
    echo "Next: ./scripts/demo.sh run    (loads the dataset on first use, then runs every test)"
  fi
}

# IDs of the VMs that start/stop act on: --vm, or every VM in the resource group.
target_ids() {
  list_vms | awk -F'\t' -v vm="$VM_OPT" 'vm == "" || $1 == vm {print $3}'
}

[[ $# -ge 1 ]] || { usage; exit 1; }
COMMAND=$1
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) RESOURCE_GROUP=$2; shift 2 ;;
    -l|--location) LOCATION=$2; shift 2 ;;
    --vm-size) VM_SIZE=$2; shift 2 ;;
    --zones) ZONES=$2; shift 2 ;;
    --ssh-key) SSH_PUBLIC_KEY_PATH=$2; shift 2 ;;
    --allow-ssh-from) ALLOW_SSH_FROM=$2; shift 2 ;;
    --vm) VM_OPT=$2; shift 2 ;;
    --options) OPTIONS=$2; shift 2 ;;
    --rounds) ROUNDS=$2; shift 2 ;;
    --iterations) ITERATIONS=$2; shift 2 ;;
    --lustre-sku) LUSTRE_SKU=$2; shift 2 ;;
    --lustre-capacity) LUSTRE_CAPACITY_TIB=$2; shift 2 ;;
    --lustre-zone) LUSTRE_ZONE=$2; shift 2 ;;
    --poll) POLL_SECONDS=$2; shift 2 ;;
    --no-wait) NO_WAIT=true; shift ;;
    --yes) YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see './scripts/demo.sh help')" ;;
  esac
done

# Resolve the VM once for single-VM commands; command substitutions below inherit it.
case "$COMMAND" in
  deploy|destroy|help|-h|--help|zones|info|start|stop|lustre-deploy|lustre-info|lustre-zones|lustre-delete) ;;
  *) VM_NAME=$(vm_name) ;;
esac

case "$COMMAND" in
  deploy) deploy ;;
  info)
    while IFS=$'\t' read -r VM_NAME zone _; do
      [[ -z "$VM_OPT" || "$VM_NAME" == "$VM_OPT" ]] || continue
      echo "== $VM_NAME${zone:+ (zone $zone)} =="
      on_vm "$BLOBPERF info"
    done < <(list_vms) ;;
  run) run_job "run-all $OPTIONS" report ;;
  load) run_job "load $OPTIONS" ;;
  latency) run_job "latency $OPTIONS" report ;;
  throughput) run_job "throughput $OPTIONS" report ;;
  zones) compare_zones ;;
  status) on_vm "$BLOBPERF status --lines 25" ;;
  wait) wait_job ;;
  report) on_vm "$BLOBPERF report --print md" ;;
  download) download ;;
  exec)
    [[ -n "$OPTIONS" ]] || die "pass the blobperf command with --options, e.g. --options cleanup"
    on_vm "$BLOBPERF $OPTIONS" ;;
  stop|start)
    vm_ids=$(target_ids)
    [[ -n "$vm_ids" ]] || die "no VM found in resource group '$RESOURCE_GROUP'"
    # shellcheck disable=SC2086 # one argument per VM ID
    # MSYS_NO_PATHCONV stops Git Bash on Windows from rewriting the /subscriptions/... IDs as file paths.
    if [[ "$COMMAND" == stop ]]; then MSYS_NO_PATHCONV=1 az vm deallocate --ids $vm_ids --output none; else MSYS_NO_PATHCONV=1 az vm start --ids $vm_ids --output none; fi
    echo "VM(s) ${COMMAND/stop/deallocat}ed: $(target_ids | awk -F/ '{print $NF}' | paste -sd, -)" ;;
  destroy)
    if [[ "$YES" != true ]]; then
      read -r -p "Delete resource group '$RESOURCE_GROUP' and everything in it? (yes/no) " answer
      [[ "$answer" == yes ]] || { echo "Cancelled."; exit 0; }
    fi
    az group delete --name "$RESOURCE_GROUP" --yes --no-wait
    echo "Deleting resource group '$RESOURCE_GROUP' in the background." ;;
  lustre-deploy) lustre_deploy ;;
  lustre-info)
    while IFS=$'\t' read -r VM_NAME zone _; do
      [[ -z "$VM_OPT" || "$VM_NAME" == "$VM_OPT" ]] || continue
      echo "== $VM_NAME${zone:+ (zone $zone)} =="
      on_vm "$LUSTREPERF info"
    done < <(list_vms) ;;
  lustre-run) TOOL=$LUSTREPERF; run_job "run-all $OPTIONS" report ;;
  lustre-zones) lustre_zones ;;
  lustre-status) on_vm "$LUSTREPERF status --lines 25" ;;
  lustre-wait) TOOL=$LUSTREPERF; wait_job ;;
  lustre-report) on_vm "$LUSTREPERF report --print md" ;;
  lustre-download) TOOL=$LUSTREPERF; download lustre ;;
  lustre-exec)
    [[ -n "$OPTIONS" ]] || die "pass the lustreperf command with --options, e.g. --options cleanup"
    on_vm "$LUSTREPERF $OPTIONS" ;;
  lustre-delete) lustre_delete ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac
