#!/bin/bash
# Prepares a benchmark VM for the Azure Managed Lustre test: Lustre client, config file and the
# lustreperf tool (which uses blobperf.py, installed by vm/install.sh, for shared helpers).
#
# Executed as root by a managed Run Command (infra/modules/lustre-setup.bicep), which replaces the
# __PLACEHOLDERS__ below at deployment time. Safe to run again: redeploying runs it again.
#
# The Lustre client is a kernel module that must match the running kernel exactly. Microsoft
# publishes prebuilt modules, signed so that they load with Secure Boot on Trusted Launch VMs, for
# specific Ubuntu kernels, and these usually lag the newest kernel of the marketplace image. When
# the running kernel has no module, this script installs a supported kernel with its module
# (preferring the LTS kernel line, as Microsoft recommends), makes it the GRUB default and reboots.
# The file system is mounted after that, by the first lustreperf command.
[ -n "${BASH_VERSION:-}" ] || exec /bin/bash "$0" "$@"
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
apt_get() { apt-get -q -o DPkg::Lock::Timeout=600 "$@"; }

log "waiting for cloud-init to finish"
cloud-init status --wait >/dev/null 2>&1 || true

install -d -m 0755 /opt/blobperf /etc/blobperf /var/lib/blobperf /var/log/blobperf

log "writing /etc/blobperf/lustre.env"
cat > /etc/blobperf/lustre.env <<'EOF'
# Written by the deployment (infra/modules/lustre-setup.bicep). LUSTREPERF_* environment variables override these.
LUSTREPERF_FILESYSTEM=__FILESYSTEM__
LUSTREPERF_MGS_ADDRESS=__MGS_ADDRESS__
LUSTREPERF_MOUNT_COMMAND=__MOUNT_COMMAND__
LUSTREPERF_SERVER_VERSION=__LUSTRE_VERSION__
LUSTREPERF_SKU=__SKU__
LUSTREPERF_CAPACITY_TIB=__CAPACITY_TIB__
LUSTREPERF_ZONE=__ZONE__
LUSTREPERF_MOUNT_POINT=/mnt/amlfs
LUSTREPERF_MDE_EXCLUDE_MOUNT=__MDE_EXCLUDE_MOUNT__
EOF

log "installing lustreperf to /opt/blobperf"
cat > /opt/blobperf/lustreperf.py.new <<'__LUSTREPERF_PY_END__'
__LUSTREPERF_PY__
__LUSTREPERF_PY_END__
python3 -m py_compile /opt/blobperf/lustreperf.py.new
mv -f /opt/blobperf/lustreperf.py.new /opt/blobperf/lustreperf.py
chmod 0755 /opt/blobperf/lustreperf.py
rm -rf /opt/blobperf/__pycache__

cat > /usr/local/bin/lustreperf <<'EOF'
#!/bin/sh
# lustreperf needs root to mount the file system, drop caches and run background jobs.
if [ "$(id -u)" -ne 0 ]; then exec sudo /usr/local/bin/lustreperf "$@"; fi
exec /usr/bin/python3 /opt/blobperf/lustreperf.py "$@"
EOF
chmod 0755 /usr/local/bin/lustreperf

# shellcheck source=/dev/null
. /etc/lsb-release
running=$(uname -r)
flavour=$(cut -d- -f3- <<<"$running")

log "configuring the Azure Managed Lustre package repository (amlfs-${DISTRIB_CODENAME})"
echo "deb [arch=$(dpkg --print-architecture)] https://packages.microsoft.com/repos/amlfs-${DISTRIB_CODENAME}/ ${DISTRIB_CODENAME} main" \
  > /etc/apt/sources.list.d/amlfs.list
curl -fsSL --retry 5 --retry-delay 5 https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.gpg.new
mv -f /etc/apt/trusted.gpg.d/microsoft.gpg.new /etc/apt/trusted.gpg.d/microsoft.gpg
for attempt in 1 2 3; do
  if apt_get update >/dev/null; then break; fi
  [ "$attempt" -lt 3 ] || { log "ERROR: apt-get update failed"; exit 1; }
  sleep 20
done

# Kernels (newest first) for which a Lustre client metapackage exists, e.g. 6.8.0-1073-azure.
kernels_for() { apt-cache madison "$1" | awk -F'|' '{gsub(/ /, "", $2); print $2}' | sort -V -r; }
installable() { apt-cache policy "linux-image-$1" 2>/dev/null | awk '/Candidate:/ {found = ($2 != "(none)")} END {exit !found}'; }

# Prints "<metapackage> <kernel>": the running kernel if a module exists for it, otherwise the kernel the
# LTS metapackage points to, then the newest one of the LTS line, then the newest installable one.
# (Lists are matched as variables: with pipefail, 'producer | grep -q' can fail when grep exits early.)
choose_client() {
  local pkg kernel kernels lts series packages candidates
  packages=$(apt-cache pkgnames amlfs-lustre-client- | grep -E '^amlfs-lustre-client-[0-9]' | sort -V -r || true)
  [ -n "$packages" ] || return 1
  for pkg in $packages; do
    kernels=$(kernels_for "$pkg")
    if grep -qxF "$running" <<<"$kernels"; then echo "$pkg $running"; return 0; fi
  done
  lts=$(apt-cache depends "linux-image-${flavour}-lts-${DISTRIB_RELEASE}" 2>/dev/null \
    | awk '/Depends: linux-image-[0-9]/ && !found {sub("linux-image-", "", $2); print $2; found = 1}' || true)
  series=${lts%%-*}
  for pkg in $packages; do
    candidates=$(kernels_for "$pkg" | grep -E -- "-${flavour}\$" || true)
    if [ -n "$lts" ] && grep -qxF "$lts" <<<"$candidates" && installable "$lts"; then echo "$pkg $lts"; return 0; fi
    if [ -n "$series" ]; then
      for kernel in $candidates; do
        if [[ "$kernel" == "$series"-* ]] && installable "$kernel"; then echo "$pkg $kernel"; return 0; fi
      done
    fi
    for kernel in $candidates; do
      if installable "$kernel"; then echo "$pkg $kernel"; return 0; fi
    done
  done
  return 1
}

# Boot this kernel by default, so neither a newer kernel from unattended upgrades nor the image's
# default kernel replaces the one that has the Lustre module.
pin_kernel() {
  local kernel=$1 entry="Ubuntu, with Linux $1"
  grep -qF "'$entry'" /boot/grub/grub.cfg || { log "ERROR: no GRUB menu entry for kernel $kernel"; exit 1; }
  if grep -qF "submenu 'Advanced options for Ubuntu'" /boot/grub/grub.cfg; then entry="Advanced options for Ubuntu>$entry"; fi
  printf '# Written by vm/lustre-install.sh: boot the kernel that has the Lustre client module.\nGRUB_DEFAULT="%s"\n' "$entry" \
    > /etc/default/grub.d/99-amlfs-lustre.cfg
  update-grub >/dev/null 2>&1
  grep -qF "set default=\"$entry\"" /boot/grub/grub.cfg || { log "ERROR: GRUB does not default to kernel $kernel"; exit 1; }
}

if modinfo -k "$running" lustre >/dev/null 2>&1 && command -v lfs >/dev/null; then
  log "Lustre client already installed for kernel $running ($(lfs --version 2>/dev/null | head -n 1))"
  pin_kernel "$running"
else
  choice=$(choose_client) || { log "ERROR: no Lustre client package for kernel $running or any installable $flavour kernel"; exit 1; }
  read -r package kernel <<<"$choice"
  if [ "$kernel" != "$running" ]; then
    log "no Lustre client module for the running kernel $running; switching to kernel $kernel"
  fi
  log "installing $package=$kernel"
  apt_get install -y "$package=$kernel" >/dev/null
  pin_kernel "$kernel"
  if [ "$kernel" != "$running" ]; then
    log "rebooting into kernel $kernel in 15 seconds; lustreperf mounts the file system on first use"
    systemd-run --on-active=15 --description="Reboot into the kernel with the Lustre client module" /bin/systemctl reboot
    echo "REBOOT-SCHEDULED kernel=$kernel"
    exit 0
  fi
fi

log "mounting the file system"
timeout 600 /usr/local/bin/lustreperf mount || log "WARNING: 'lustreperf mount' failed; see the output above"
log "setup complete"
