#!/bin/bash
# Prepares the benchmark VM: config file, blobperf tool, RAM disk and AzCopy.
#
# Executed as root by the Azure Custom Script Extension (infra/modules/vm-setup.bicep), which
# replaces the __PLACEHOLDERS__ below at deployment time. Safe to run again: every redeployment
# that changes the tool or the configuration re-runs it.
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
set -euo pipefail

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

log "waiting for cloud-init to finish"
cloud-init status --wait >/dev/null 2>&1 || true

install -d -m 0755 /opt/blobperf /etc/blobperf /var/lib/blobperf /var/log/blobperf

log "writing /etc/blobperf/blobperf.env"
cat > /etc/blobperf/blobperf.env <<'EOF'
# Written by the deployment (infra/modules/vm-setup.bicep). BLOBPERF_* environment variables override these.
BLOBPERF_PREMIUM_ACCOUNT=__PREMIUM_ACCOUNT__
BLOBPERF_PREMIUM_ENDPOINT=__PREMIUM_ENDPOINT__
BLOBPERF_STANDARD_ACCOUNT=__STANDARD_ACCOUNT__
BLOBPERF_STANDARD_ENDPOINT=__STANDARD_ENDPOINT__
BLOBPERF_WORKDIR=/ramdisk/blobperf
BLOBPERF_MDE_EXCLUDE_WORKDIR=__MDE_EXCLUDE_WORKDIR__
EOF

log "installing blobperf to /opt/blobperf"
cat > /opt/blobperf/blobperf.py.new <<'__BLOBPERF_PY_END__'
__BLOBPERF_PY__
__BLOBPERF_PY_END__
python3 -m py_compile /opt/blobperf/blobperf.py.new
mv -f /opt/blobperf/blobperf.py.new /opt/blobperf/blobperf.py
chmod 0755 /opt/blobperf/blobperf.py
rm -rf /opt/blobperf/__pycache__

cat > /usr/local/bin/blobperf <<'EOF'
#!/bin/sh
# blobperf needs root for the RAM disk, the results folder and background jobs.
if [ "$(id -u)" -ne 0 ]; then exec sudo /usr/local/bin/blobperf "$@"; fi
exec /usr/bin/python3 /opt/blobperf/blobperf.py "$@"
EOF
chmod 0755 /usr/local/bin/blobperf

# The blog post used a RAM disk so local disk speed cannot limit the AzCopy tests.
log "mounting the RAM disk at /ramdisk"
mkdir -p /ramdisk
if ! grep -q '[[:space:]]/ramdisk[[:space:]]' /etc/fstab; then
  echo 'tmpfs /ramdisk tmpfs defaults,noatime,mode=1777,size=50% 0 0' >> /etc/fstab
fi
mountpoint -q /ramdisk || mount /ramdisk

install_azcopy() {
  local url tmp
  case "$(uname -m)" in
    x86_64) url=https://aka.ms/downloadazcopy-v10-linux ;;
    aarch64) url=https://aka.ms/downloadazcopy-v10-linux-arm64 ;;
    *) echo "unsupported architecture $(uname -m)" >&2; return 1 ;;
  esac
  tmp=$(mktemp -d)
  curl -fsSL --retry 5 --retry-delay 5 -o "$tmp/azcopy.tar.gz" "$url"
  tar -xzf "$tmp/azcopy.tar.gz" -C "$tmp"
  install -m 0755 "$(find "$tmp" -type f -name azcopy | head -n 1)" /usr/local/bin/azcopy
  rm -rf "$tmp"
}
if [ ! -x /usr/local/bin/azcopy ] || [ "${BLOBPERF_REINSTALL_AZCOPY:-false}" = "true" ]; then
  log "installing the latest AzCopy v10"
  install_azcopy
fi
log "$(/usr/local/bin/azcopy --version 2>/dev/null | head -n 1)"

# Role assignments can take a few minutes to apply; the tests wait for access, so this is informational.
log "checking access to the storage accounts"
timeout 120 /usr/local/bin/blobperf info || log "WARNING: 'blobperf info' failed; see the output above"
log "setup complete"
