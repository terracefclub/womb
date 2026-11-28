#!/usr/bin/env bash
# One complete VM rehearsal of the production lease host's run sheet (docs/cloud-rehearsal.md, "VM rehearsal of the
# host steps"): tools from the repository's pinned nixpkgs, the kexec archive built from this checkout, then
# test_lease_host_vm.py under the shared heavy lock, then the run's disks and keys purged by exact path. Linux build
# machine only; never in CI.
#
#   infra/nixos/lease-host-vm-run.sh RUN_NAME EVIDENCE_DIR TFC_SOURCE NEXT_RELEASE_DIR PREVIOUS_RELEASE_DIR DEBIAN_IMAGE PORT
#
# RUN_NAME      the run's name; its disks and keys live in ~/work/terrace-rehearsal/RUN_NAME (scratch, not snapshotted)
# EVIDENCE_DIR  a new directory for the evidence (kept); steps.log there has one line per stage with its exit
# TFC_SOURCE    a website checkout at the pinned revision (platform/website-release.json)
# NEXT_RELEASE_DIR      holds the kept web and CMS archives of the pinned release (platform/website-release.json)
# PREVIOUS_RELEASE_DIR  the same for the previous release (platform/production/deploy-previous-release.json)
# DEBIAN_IMAGE  the pinned Debian 13 genericcloud amd64 QCOW2 (docs/cloud-rescue-install.md, "Offline trust proof")
# PORT          a free loopback port for the guest's SSH forward
#
# Run it from a clean checkout of the revision under test (the script's own repository).
set -uo pipefail
[[ $# == 7 ]] || { sed -n 2,17p "$0" >&2; exit 2; }
NAME=$1 E=$2 T=$3 RN=$4 RP=$5 BASE=$6 PORT=$7
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SCRATCH=$HOME/work/terrace-rehearsal
OUT=$SCRATCH/$NAME
LOCK=$HOME/.falcon-heavy.lock
[[ $NAME =~ ^[a-z0-9-]+$ && ! -e $E && ! -e $OUT ]] || { echo "refused: bad run name, or evidence/output exists" >&2; exit 2; }
install -d -m 0700 "$E" "$SCRATCH"
log() { echo "$(date -u +%FT%TZ) $*" >>"$E/steps.log"; }
stage() { local name=$1; shift; local rc; "$@" >"$E/$name.out" 2>&1; rc=$?; log "$name exit $rc"; return $rc; }
echo $$ >"$E/runner.pid"
HEAD=$(git -C "$REPO" rev-parse HEAD)
log "start head $HEAD run $NAME"
stage clean-checkout bash -c 'test -z "$(git -C "$1" status --porcelain --ignored)"' _ "$REPO" || { log "stopped: checkout not clean"; exit 1; }
# At least 100 GB free on both the scratch and the home dataset (WORKER rules).
space() {
  local path avail
  for path in "$SCRATCH" "$HOME/terrace-work"; do
    avail=$(df -B1 --output=avail "$path" | tail -n 1)
    echo "$path $avail"
    (( avail >= 100 * 1000 * 1000 * 1000 )) || return 1
  done
}
stage space space || { log "stopped: less than 100 GB free"; exit 1; }
# One named output each (a multi-output package prints every output otherwise, man pages first).
pin() { nix build --no-link --print-out-paths --impure --option builders "" --expr \
  "(import (builtins.getFlake \"path:$REPO/platform\").inputs.nixpkgs {system = \"x86_64-linux\";}).$1"; }
tools() {
  set -e
  {
    echo "qemu=$(pin qemu_kvm.out)"
    echo "ovmf=$(pin OVMF.fd)"
    echo "openssl=$(pin openssl.bin)"
    echo "openssh=$(pin openssh.out)"
    echo "rclone=$(pin rclone.out)"
    echo "restic=$(pin restic.out)"
    echo "sops=$(pin sops.out)"
    echo "dosfstools=$(pin dosfstools.out)"
    echo "mtools=$(pin mtools.out)"
    echo "secrets=$(nix build --no-link --print-out-paths --option builders "" "path:$REPO/platform#terrace-secrets")"
    echo "archive=$(nix build --no-link --print-out-paths --impure --option builders "" --file "$REPO/infra/nixos/kexec-installer.nix" archive)"
  } >"$E/tools.env"
}
stage tools flock -w 28800 "$LOCK" bash -c "$(declare -f pin tools); REPO=$REPO E=$E; tools" || { log "stopped: tools"; exit 1; }
# shellcheck disable=SC1091
source "$E/tools.env"
# Seconds, not hours: every tool is one executable file, and `prepare` works in a scratch run directory.
executables() {
  local tool
  for tool in "$qemu/bin/qemu-system-x86_64" "$qemu/bin/qemu-img" "$openssl/bin/openssl" "$openssh/bin/ssh" \
    "$openssh/bin/ssh-keygen" "$rclone/bin/rclone" "$restic/bin/restic" "$sops/bin/sops" "$dosfstools/bin/mkfs.vfat" \
    "$mtools/bin/mcopy" "$secrets/bin/terrace-secrets"; do
    if [[ -f $tool && -x $tool ]]; then echo "ok $tool"; else echo "NOT AN EXECUTABLE FILE: $tool"; return 1; fi
  done
  for tool in "$ovmf/FV/OVMF_CODE.fd" "$ovmf/FV/OVMF_VARS.fd"; do
    [[ -f $tool ]] && echo "ok $tool" || { echo "MISSING: $tool"; return 1; }
  done
  ls "$archive"/*.tar.gz
}
stage executables executables || { log "stopped: tools not executable"; exit 1; }
pick() { ls "$1"/terrace-"$2"-"$3"-*.tar.gz; }
NEXT=$(jq -r .revision "$REPO/platform/website-release.json")
PREV=$(jq -r .revision "$REPO/platform/production/deploy-previous-release.json")
preflight() {
  local dir=$SCRATCH/$NAME-preflight rc
  install -d -m 0700 "$dir"
  PATH="$openssh/bin:$PATH" python3 -B "$REPO/infra/nixos/lease_host_steps.py" prepare "$dir" \
    --terrace-secrets "$secrets/bin/terrace-secrets" --openssl "$openssl/bin/openssl" --ssh-keygen "$openssh/bin/ssh-keygen" \
    --web-archive "$(pick "$RN" web "$NEXT")" --cms-archive "$(pick "$RN" cms "$NEXT")" \
    --previous-web-archive "$(pick "$RP" web "$PREV")" --previous-cms-archive "$(pick "$RP" cms "$PREV")" \
    --tfc-source "$T" --womb-revision "$HEAD"
  rc=$?
  rm -rf -- "$dir" && test ! -e "$dir" || return 1
  return $rc
}
stage preflight preflight || { log "stopped: prepare failed in the scratch run directory"; exit 1; }
# The evaluation check: the lease host and its seven variants must hold the profile's invariants.
stage eval-check flock -w 28800 "$LOCK" bash -c 'cd "$1" && nix eval --raw --impure --option builders "" --file infra/nixos/production-eval-check.nix | tee /dev/stderr | grep -q "^PASS"' _ "$REPO" \
  || { log "stopped: evaluation check"; exit 1; }
ARCHIVE=$(ls "$archive"/*.tar.gz)
ARCHIVE_SHA=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
echo "$ARCHIVE_SHA  $(basename "$ARCHIVE")" >"$E/archive.sha256"
# The host steps run about 90 to 130 minutes after the fixture starts. Keep them clear of the writer timers the
# update steps guard against (driver: no writer timer within 10 minutes): the backup at 03:15 UTC (up to 15 minutes'
# delay), the sweep at 03:30 and the weekly check on Sundays at 05:30 (up to 30 minutes' delay). Wait instead of
# failing a step at its guard.
timer_window() {
  local minute day
  while :; do
    minute=$((10#$(date -u +%H) * 60 + 10#$(date -u +%M))) day=$(date -u +%u)
    if (( minute >= 45 && minute < 230 )) || { (( day == 7 )) && (( minute >= 180 && minute < 365 )); }; then
      echo "$(date -u +%FT%TZ) waiting: the host steps would meet a writer timer"; sleep 300
    else
      return 0
    fi
  done
}
stage timer-window timer_window
install -d -m 0700 "$OUT"
stage fixture flock -w 28800 "$LOCK" env PATH="$openssh/bin:$PATH" python3 -B "$REPO/infra/nixos/test_lease_host_vm.py" "$OUT" \
  --evidence "$E/vm" --checkout "$REPO" --base "$BASE" --archive "$ARCHIVE" --archive-sha256 "$ARCHIVE_SHA" --port "$PORT" \
  --qemu "$qemu/bin/qemu-system-x86_64" --qemu-img "$qemu/bin/qemu-img" --mkfs-vfat "$dosfstools/bin/mkfs.vfat" \
  --mcopy "$mtools/bin/mcopy" --ovmf-code "$ovmf/FV/OVMF_CODE.fd" --ovmf-vars "$ovmf/FV/OVMF_VARS.fd" \
  --openssl "$openssl/bin/openssl" --ssh-keygen "$openssh/bin/ssh-keygen" --terrace-secrets "$secrets/bin/terrace-secrets" \
  --rclone "$rclone/bin/rclone" --restic "$restic/bin/restic" --sops "$sops/bin/sops" \
  --web-archive "$(pick "$RN" web "$NEXT")" --cms-archive "$(pick "$RN" cms "$NEXT")" \
  --previous-web-archive "$(pick "$RP" web "$PREV")" --previous-cms-archive "$(pick "$RP" cms "$PREV")" --tfc-source "$T"
result=$?
# Nothing may be left running: the guest (its PID from the fixture), the driver's forwards and the SFTP units.
verify_off() {
  local pid
  pid=$(jq -r '.qemuPid // empty' "$E/vm/fixture.json" 2>/dev/null)
  if [[ -n $pid && -e /proc/$pid ]] && grep -q qemu "/proc/$pid/comm" 2>/dev/null; then echo "guest $pid still running"; return 1; fi
  if systemctl --user list-units --all --plain --no-legend 'terrace-lease-sftp-*' | grep -q .; then echo "SFTP unit left"; return 1; fi
  echo "guest, forwards and SFTP units gone"
}
stage verify-off verify_off
# Purge the run's disks, keys and synthetic data by exact path (pass or fail), then confirm.
stage purge bash -c 'rm -rf -- "$1" && test ! -e "$1"' _ "$OUT"
stage sizes du -sh "$E"
(cd "$E" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
log "done fixture exit $result"
exit "$result"
