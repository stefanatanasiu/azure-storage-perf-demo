#!/usr/bin/env bash
# Deploy and run the Azure Premium vs Standard block blob performance demo (bash + Azure CLI).
# Same commands as scripts/demo.ps1. Run './scripts/demo.sh help' for usage.
set -euo pipefail

RESOURCE_GROUP=${RESOURCE_GROUP:-rg-blobperf-demo}
LOCATION=${LOCATION:-swedencentral}
VM_SIZE=${VM_SIZE:-}
SSH_PUBLIC_KEY_PATH=${SSH_PUBLIC_KEY_PATH:-}
ALLOW_SSH_FROM=${ALLOW_SSH_FROM:-}
OPTIONS=${OPTIONS:-}
POLL_SECONDS=${POLL_SECONDS:-60}
NO_WAIT=false
YES=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BLOBPERF=/usr/local/bin/blobperf
VM_NAME=""

usage() {
  cat <<'EOF'
Usage: ./scripts/demo.sh <command> [options]

Commands:
  deploy      Deploy or update the resource group, network, storage accounts and VM.
  info        VM details and access to both storage accounts.
  run         Load the dataset if needed, run every test and print the summary (blobperf run-all).
  load        Load the 1,000,000 x 64 KiB latency dataset only.
  latency     Latency test only.
  throughput  AzCopy upload/download and list test only.
  status      Progress of the current or last job.
  wait        Follow the current job until it finishes.
  report      Print the latest summary.
  download    Save the latest report (summary.md/.json, report.html, raw results) under ./results.
  exec        Run any blobperf command on the VM, e.g. --options cleanup
  stop        Deallocate the VM (storage accounts and data are kept).
  start       Start the VM again.
  destroy     Delete the resource group and everything in it.

Options (or the environment variables in brackets):
  -g, --resource-group NAME   resource group (RESOURCE_GROUP, default rg-blobperf-demo)
  -l, --location REGION       region for deploy (LOCATION, default swedencentral)
  --vm-size SIZE              VM size for deploy (VM_SIZE, default Standard_D32s_v6)
  --ssh-key PATH              SSH public key for deploy (SSH_PUBLIC_KEY_PATH)
  --allow-ssh-from CIDR|me    open SSH to this source on deploy (ALLOW_SSH_FROM, default: no inbound access)
  --options "..."             extra blobperf options, e.g. "--iterations 1" (OPTIONS)
  --poll SECONDS              seconds between progress checks (POLL_SECONDS, default 60)
  --no-wait                   start the job and return
  --yes                       do not ask for confirmation in destroy
EOF
}

die() { echo "error: $*" >&2; exit 1; }

vm_name() {
  if [[ -z "$VM_NAME" ]]; then
    VM_NAME=$(az vm list --resource-group "$RESOURCE_GROUP" --query '[0].name' --output tsv)
    [[ -n "$VM_NAME" ]] || die "no VM in resource group '$RESOURCE_GROUP'; run './scripts/demo.sh deploy' first"
  fi
  echo "$VM_NAME"
}

# Runs a shell script on the VM as root and prints its stdout (run-command keeps ~4 KB of output).
on_vm() {
  local script message name
  name=$(vm_name)
  script=$(mktemp)
  printf '#!/bin/bash\n%s\n' "$1" > "$script"
  message=$(az vm run-command invoke --resource-group "$RESOURCE_GROUP" --name "$name" \
    --command-id RunShellScript --scripts @"$script" --query 'value[0].message' --output tsv)
  rm -f "$script"
  printf '%s\n' "$message" | awk '/^\[stdout\]/{out=1; next} /^\[stderr\]/{out=0; err=1; next} out{print} err && NF{print > "/dev/stderr"}'
}

start_job() {
  local output
  output=$(on_vm "$BLOBPERF start $1 $OPTIONS")
  echo "$output"
  [[ "$output" == *"started job"* ]] || die "the job did not start (see the output above)"
}

wait_job() {
  local seen output state
  seen=$(mktemp)
  while true; do
    output=$(on_vm "$BLOBPERF status --lines 12")
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
    echo "Follow it with: ./scripts/demo.sh wait -g $RESOURCE_GROUP"
    return
  fi
  wait_job || die "the job did not succeed; see './scripts/demo.sh status'"
  if [[ "${2:-}" == report ]]; then on_vm "$BLOBPERF report --print md"; fi
}

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# GNU coreutils, BSD/macOS and OpenSSL spell "decode" differently; an unknown option fails before any output.
b64decode() { base64 --decode < "$1" 2>/dev/null || base64 -D < "$1" 2>/dev/null || openssl base64 -d -A < "$1"; }

download() {
  local i=0 total=1 sha="" output header destination encoded
  destination="$REPO_ROOT/results/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$destination"
  encoded="$destination/bundle.b64"
  : > "$encoded"
  while (( i < total )); do
    output=$(on_vm "$BLOBPERF export --chunk $i")
    header=$(head -n 1 <<<"$output")
    [[ "$header" =~ BLOBPERF-EXPORT\ chunk=[0-9]+\ total=([0-9]+)\ sha256=([0-9a-f]{64}) ]] || die "unexpected export output: $output"
    total=${BASH_REMATCH[1]}
    sha=${BASH_REMATCH[2]}
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
}

ssh_public_key() {
  local candidate tmp existing
  # Azure cannot change the key of an existing VM by redeploying, so keep whatever it already has.
  existing=$(az vm list --resource-group "$RESOURCE_GROUP" \
    --query '[0].osProfile.linuxConfiguration.ssh.publicKeys[0].keyData' --output tsv 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    [[ -z "$SSH_PUBLIC_KEY_PATH" ]] || echo "warning: the VM already exists and keeps its current SSH key (change it with 'az vm user update')." >&2
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
  echo "No SSH key found: using a throwaway key (the VM is managed with run-command only)." >&2
  tmp=$(mktemp -d)
  ssh-keygen -q -t ed25519 -N '' -C blobperf-throwaway -f "$tmp/key"
  cat "$tmp/key.pub"
  rm -rf "$tmp"
}

deploy() {
  local key cidr params
  key=$(ssh_public_key)
  params=(location="$LOCATION" resourceGroupName="$RESOURCE_GROUP" adminSshPublicKey="$key")
  [[ -z "$VM_SIZE" ]] || params+=(vmSize="$VM_SIZE")
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
    --query '{vm:properties.outputs.vmName.value, premium:properties.outputs.premiumAccountName.value, standard:properties.outputs.standardAccountName.value, sshIp:properties.outputs.vmPublicIpAddress.value}' \
    --output table
  echo
  echo "Next: ./scripts/demo.sh run    (loads the dataset on first use, then runs every test)"
}

[[ $# -ge 1 ]] || { usage; exit 1; }
COMMAND=$1
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) RESOURCE_GROUP=$2; shift 2 ;;
    -l|--location) LOCATION=$2; shift 2 ;;
    --vm-size) VM_SIZE=$2; shift 2 ;;
    --ssh-key) SSH_PUBLIC_KEY_PATH=$2; shift 2 ;;
    --allow-ssh-from) ALLOW_SSH_FROM=$2; shift 2 ;;
    --options) OPTIONS=$2; shift 2 ;;
    --poll) POLL_SECONDS=$2; shift 2 ;;
    --no-wait) NO_WAIT=true; shift ;;
    --yes) YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option $1 (see './scripts/demo.sh help')" ;;
  esac
done

# Resolve the VM once; command substitutions below inherit it.
case "$COMMAND" in
  deploy|destroy|help|-h|--help) ;;
  *) VM_NAME=$(vm_name) ;;
esac

case "$COMMAND" in
  deploy) deploy ;;
  info) on_vm "$BLOBPERF info" ;;
  run) run_job run-all report ;;
  load) run_job load ;;
  latency) run_job latency report ;;
  throughput) run_job throughput report ;;
  status) on_vm "$BLOBPERF status --lines 25" ;;
  wait) wait_job ;;
  report) on_vm "$BLOBPERF report --print md" ;;
  download) download ;;
  exec)
    [[ -n "$OPTIONS" ]] || die "pass the blobperf command with --options, e.g. --options cleanup"
    on_vm "$BLOBPERF $OPTIONS" ;;
  stop) az vm deallocate --resource-group "$RESOURCE_GROUP" --name "$(vm_name)" --output none && echo "VM deallocated." ;;
  start) az vm start --resource-group "$RESOURCE_GROUP" --name "$(vm_name)" --output none && echo "VM started." ;;
  destroy)
    if [[ "$YES" != true ]]; then
      read -r -p "Delete resource group '$RESOURCE_GROUP' and everything in it? (yes/no) " answer
      [[ "$answer" == yes ]] || { echo "Cancelled."; exit 0; }
    fi
    az group delete --name "$RESOURCE_GROUP" --yes --no-wait
    echo "Deleting resource group '$RESOURCE_GROUP' in the background." ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac
