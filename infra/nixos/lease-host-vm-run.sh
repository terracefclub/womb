#!/usr/bin/env bash
# One complete VM rehearsal of the production lease host's run sheet (docs/cloud-rehearsal.md, "VM rehearsal of the
# host steps"): tools from the repository's pinned nixpkgs, the kexec archive built from this checkout, then
# test_lease_host_vm.py under the shared heavy lock, then the run's disks and keys purged by exact path. Linux build
# machine only; never in CI.
#
#   BUILD_LOCK=/path/to/shared-heavy.lock infra/nixos/lease-host-vm-run.sh RUN_NAME EVIDENCE_DIR TFC_SOURCE NEXT_RELEASE_DIR PREVIOUS_RELEASE_DIR DEBIAN_IMAGE PORT
#
# RUN_NAME      the run's name; its disks and keys live in ~/work/terrace-rehearsal/RUN_NAME (scratch, not snapshotted)
# EVIDENCE_DIR  a new directory for the evidence (kept); steps.log there has one line per stage with its exit;
#               SHA256SUMS is written last and nothing is appended to a summed file after it (after-sums.log)
# TFC_SOURCE    a website checkout at the pinned revision (platform/website-release.json)
# NEXT_RELEASE_DIR      holds the kept web and CMS archives of the pinned release (platform/website-release.json)
# PREVIOUS_RELEASE_DIR  the same for the previous release (platform/production/deploy-previous-release.json)
# DEBIAN_IMAGE  the pinned Debian 13 genericcloud amd64 QCOW2 (docs/cloud-rescue-install.md, "Offline trust proof")
# PORT          a free loopback port for the guest's SSH forward
#
# BUILD_LOCK    required: the build machine's shared lock file, held for the evaluation, the builds and the VM run
# Run it from a clean checkout of the revision under test (the script's own repository).
set -uo pipefail
[[ $# == 7 ]] || { sed -n 2,19p "$0" >&2; exit 2; }
NAME=$1 E=$2 T=$3 RN=$4 RP=$5 BASE=$6 PORT=$7
[[ -n ${HOME:-} && $HOME == /* ]] || { echo "refused: HOME must be an absolute path" >&2; exit 2; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SCRATCH=$HOME/work/terrace-rehearsal
OUT=$SCRATCH/$NAME
LOCK=${BUILD_LOCK:-}
[[ $LOCK == /* ]] || { echo "refused: set BUILD_LOCK to the shared build lock file (an absolute path)" >&2; exit 2; }
[[ $NAME =~ ^[a-z0-9-]+$ && ! -e $E && ! -e $OUT ]] || { echo "refused: bad run name, or evidence/output exists" >&2; exit 2; }
install -d -m 0700 "$E" "$SCRATCH"
log() { echo "$(date -u +%FT%TZ) $*" >>"$E/steps.log"; }
stage() { local name=$1; shift; local rc; "$@" >"$E/$name.out" 2>&1; rc=$?; log "$name exit $rc"; return $rc; }
echo $$ >"$E/runner.pid"
# A signal stops the fixture (it stops its guest and the driver's clean-up runs), then the verification and the
# purge still run; nothing after the first signal starts a new stage.
interrupted=0 child="" fixture_started=0
trap 'interrupted=1; [[ -n $child ]] && kill -TERM "$child" 2>/dev/null' INT TERM HUP
# Nothing may be left running: the guest (its PID and its own off-check from fixture.json), the driver's forwards
# and the SFTP units. A fixture that started and left no fixture.json cannot be verified off.
verify_off() {
  local pid off
  if (( fixture_started )); then
    [[ -f $E/vm/fixture.json ]] || { echo "fixture.json missing: the guest cannot be verified off"; return 1; }
    pid=$(jq -r '.qemuPid // empty' "$E/vm/fixture.json") off=$(jq -r '.qemuOff // false' "$E/vm/fixture.json")
    if [[ -n $pid && -e /proc/$pid ]] && grep -q qemu "/proc/$pid/comm" 2>/dev/null; then echo "guest $pid still running"; return 1; fi
    [[ -z $pid || $off == true ]] || { echo "the fixture did not verify its guest off"; return 1; }
  fi
  local units
  units=$(systemctl --user list-units --all --plain --no-legend 'terrace-lease-sftp-*') || { echo "cannot list user units"; return 1; }
  [[ -z $units ]] || { echo "SFTP unit left: $units"; return 1; }
  echo "guest, forwards and SFTP units gone"
}
# The end of every path: verify, purge only what is verified off, then the sums, last.
finish() {
  local code=$1
  if stage verify-off verify_off; then
    # Purge the run's disks, keys and synthetic data by exact path (pass or fail), then confirm.
    stage purge bash -c 'rm -rf -- "$1" && test ! -e "$1"' _ "$OUT" || code=3
  else
    log "NOT purged: the guest or a unit is not verified off; $OUT kept, escalate"
    code=3
  fi
  (( interrupted )) && (( code == 0 )) && code=4
  stage sizes du -sh "$E"
  log "done exit $code (fixture ${result:-not run})"
  (cd "$E" && find . -type f ! -name SHA256SUMS ! -name after-sums.log -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
  exit "$code"
}
gate() { (( ! interrupted )) || { log "stopped: interrupted"; finish 4; }; }
HEAD=$(git -C "$REPO" rev-parse HEAD)
log "start head $HEAD run $NAME"
stage clean-checkout bash -c 'test -z "$(git -C "$1" status --porcelain --ignored)"' _ "$REPO" || { log "stopped: checkout not clean"; finish 1; }
# At least 100 GB free on both the scratch and the home dataset (WORKER rules).
space() {
  local path avail
  for path in "$SCRATCH" "$HOME/terrace-work"; do
    avail=$(df -B1 --output=avail "$path" | tail -n 1)
    echo "$path $avail"
    (( avail >= 100 * 1000 * 1000 * 1000 )) || return 1
  done
}
stage space space || { log "stopped: less than 100 GB free"; finish 1; }
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
gate
stage tools flock -w 28800 "$LOCK" env REPO="$REPO" E="$E" bash -c "$(declare -f pin tools); tools" || { log "stopped: tools"; finish 1; }
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
stage executables executables || { log "stopped: tools not executable"; finish 1; }
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
gate
stage preflight preflight || { log "stopped: prepare failed in the scratch run directory"; finish 1; }
gate
# The evaluation check: the lease host and its seven variants must hold the profile's invariants.
stage eval-check flock -w 28800 "$LOCK" bash -c 'cd "$1" && nix eval --raw --impure --option builders "" --file infra/nixos/production-eval-check.nix | tee /dev/stderr | grep -q "^PASS"' _ "$REPO" \
  || { log "stopped: evaluation check"; finish 1; }
gate
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
gate
install -d -m 0700 "$OUT"
# The fixture is this shell's own child (the lock is taken on a descriptor), so a signal reaches it directly.
fixture() {
  exec 9>>"$LOCK"
  flock -w 28800 9 || { exec 9>&-; return 75; }
  fixture_started=1
  "$@" & child=$!
  local rc
  while :; do wait "$child"; rc=$?; kill -0 "$child" 2>/dev/null || break; done
  child=""
  exec 9>&-
  return "$rc"
}
stage fixture fixture env PATH="$openssh/bin:$PATH" python3 -B "$REPO/infra/nixos/test_lease_host_vm.py" "$OUT" \
  --evidence "$E/vm" --checkout "$REPO" --base "$BASE" --archive "$ARCHIVE" --archive-sha256 "$ARCHIVE_SHA" --port "$PORT" \
  --qemu "$qemu/bin/qemu-system-x86_64" --qemu-img "$qemu/bin/qemu-img" --mkfs-vfat "$dosfstools/bin/mkfs.vfat" \
  --mcopy "$mtools/bin/mcopy" --ovmf-code "$ovmf/FV/OVMF_CODE.fd" --ovmf-vars "$ovmf/FV/OVMF_VARS.fd" \
  --openssl "$openssl/bin/openssl" --ssh-keygen "$openssh/bin/ssh-keygen" --terrace-secrets "$secrets/bin/terrace-secrets" \
  --rclone "$rclone/bin/rclone" --restic "$restic/bin/restic" --sops "$sops/bin/sops" \
  --web-archive "$(pick "$RN" web "$NEXT")" --cms-archive "$(pick "$RN" cms "$NEXT")" \
  --previous-web-archive "$(pick "$RP" web "$PREV")" --previous-cms-archive "$(pick "$RP" cms "$PREV")" --tfc-source "$T"
result=$?
finish "$result"
