"""Enter and verify the RAM installer through an independently pinned stock host."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile

import rescue_install as install


ARCHIVE_LIMIT = 1_073_741_824
ARCHIVE_SHA = re.compile(r"[a-f0-9]{64}")
ARCHIVE_FILES = {"kexec/bzImage", "kexec/initrd", "kexec/ip", "kexec/kexec", "kexec/run"}
SCRATCH = "/run/terrace-stock-kexec"
STOCK_PROBE = """
set -euo pipefail
test "$(id -u)" = 0 && test "$(uname -m)" = x86_64
test ! -e /sys/firmware/efi
grep -qx 'ID=debian' /etc/os-release
grep -Eq '^VERSION_ID="?13"?$' /etc/os-release
test "$(findmnt -rn -o FSTYPE --mountpoint /run)" = tmpfs
cloud-init status --wait >/dev/null
read -r client _ <<<"${SSH_CLIENT:?}"
settings="$(/usr/sbin/sshd -T -C "user=root,host=$client,addr=$client")"
grep -qx 'pubkeyauthentication yes' <<<"$settings"
grep -qx 'authenticationmethods publickey' <<<"$settings"
grep -qx 'allowusers root' <<<"$settings"
grep -qx 'passwordauthentication no' <<<"$settings"
grep -qx 'kbdinteractiveauthentication no' <<<"$settings"
grep -qx 'hostbasedauthentication no' <<<"$settings"
grep -qx 'gssapiauthentication no' <<<"$settings"
grep -Eq '^permitrootlogin (prohibit-password|without-password)$' <<<"$settings"
test "$(grep '^hostkey ' <<<"$settings")" = 'hostkey /etc/ssh/ssh_host_ed25519_key'
test -x /usr/bin/ip && test "$(stat -Lc '%u:%g:%a' /usr/bin/ip)" = 0:0:755
/usr/bin/ip --json addr >/dev/null
command -v cpio >/dev/null
keys=(/etc/ssh/ssh_host_*)
test "${#keys[@]}" = 2
test "${keys[0]}" = /etc/ssh/ssh_host_ed25519_key
test "${keys[1]}" = /etc/ssh/ssh_host_ed25519_key.pub
for key in "${keys[@]}"; do
  test -f "$key" && test ! -L "$key"
done
test "$(stat -c '%u:%a:%h' "${keys[0]}")" = 0:600:1
read -r kind key _ < <(ssh-keygen -y -f "${keys[0]}")
test "$kind $key" = "ssh-ed25519 $1"
mapfile -t -n 2 published <"${keys[1]}"
test "${#published[@]}" = 1
test "${published[0]}" = "ssh-ed25519 $1"
auth=/root/.ssh/authorized_keys
test -f "$auth" && test ! -L "$auth"
test "$(stat -c '%u:%h' "$auth")" = 0:1
test ! -e /root/.ssh/authorized_keys2 && test ! -L /root/.ssh/authorized_keys2
test ! -e /etc/ssh/authorized_keys.d/root && test ! -L /etc/ssh/authorized_keys.d/root
mapfile -t lines < "$auth"
test "${#lines[@]}" = 1
read -r kind key _ <<<"${lines[0]}"
test "$kind $key" = "ssh-ed25519 $2"
cat /proc/sys/kernel/random/boot_id
"""
UPLOAD = f"""
set -euo pipefail
umask 077
test "$(cat /proc/sys/kernel/random/boot_id)" = "$1"
test "$(findmnt -rn -o FSTYPE --mountpoint /run)" = tmpfs
test "$(awk '/^MemAvailable:/ {{print $2}}' /proc/meminfo)" -gt "$(( ($3 + 2 * $4) / 1024 + 524288 ))"
page_size="$(getconf PAGESIZE)"
limit="$(( ($3 + $4 + 16777216 + page_size - 1) / page_size * page_size ))"
scratch={SCRATCH}
mkdir -m 0700 -- "$scratch"
cleanup() {{
  status=$?
  cd /
  if mountpoint -q -- "$scratch"; then
    test "$(findmnt -rn -o FSTYPE --mountpoint "$scratch")" = tmpfs && umount -- "$scratch" || status=1
  fi
  rmdir -- "$scratch" || status=1
  exit "$status"
}}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
mount -t tmpfs -o "size=$limit,mode=0700,nosuid,nodev" tmpfs "$scratch"
test "$(findmnt -rn -o TARGET,FSTYPE --mountpoint "$scratch")" = "$scratch tmpfs"
test "$(stat -c '%u:%g:%a' "$scratch")" = 0:0:700
options="$(findmnt -rn -o OPTIONS --mountpoint "$scratch")"
[[ ",$options," == *,nosuid,* && ",$options," == *,nodev,* && ",$options," != *,noexec,* ]]
test "$(df -PB1 "$scratch" | awk 'NR == 2 {{print $2}}')" -eq "$limit"
cd "$scratch"
head -c "$(( $3 + 1 ))" >installer.tar.gz
test "$(stat -c %s installer.tar.gz)" = "$3"
printf '%s  installer.tar.gz\n' "$2" | sha256sum --check --status
tar --no-same-owner -xzf installer.tar.gz
rm -- installer.tar.gz
test -x kexec/run && test -f kexec/run && test ! -L kexec/run
trap - EXIT
"""
KEXEC = f"""
set -euo pipefail
umask 077
test "$(cat /proc/sys/kernel/random/boot_id)" = "$1"
test "$(findmnt -rn -o TARGET,FSTYPE --mountpoint {SCRATCH})" = "{SCRATCH} tmpfs"
test "$(stat -c '%u:%g:%a' {SCRATCH})" = 0:0:700
cd {SCRATCH}
# The upstream runner copies /etc/machine-id with plain cp. Under umask 077 the
# RAM system gets a root-only ID, networkd cannot start its DHCP clients and the
# restored link fails. Its scratch stays in this 0700 tmpfs; cp -a keeps key modes.
umask 022
exec setsid --wait timeout --kill-after=5 90 ./kexec/run
"""


def boot_id(data):
    value = data.decode("ascii").strip()
    install.require(install.BOOT_ID.fullmatch(value), "invalid stock boot identity")
    return value


def stock_boot(ssh, key, directory):
    public = install.checked_process(
        ["ssh-keygen", "-y", "-f", str(directory / "operator-ed25519")], 5, capture=True
    ).decode("ascii").split()
    install.require(len(public) >= 2 and public[0] == "ssh-ed25519", "invalid operator key")
    return boot_id(install.remote(ssh, 60, STOCK_PROBE, key, public[1], capture=True))


def archive_unpacked_bytes(source):
    seen = set()
    total = 0
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            install.require(member.name not in seen and (
                (member.name == "kexec" and member.isdir() and member.size == 0)
                or (member.name in ARCHIVE_FILES and member.type == tarfile.REGTYPE and member.size > 0)
            ), "unexpected installer archive entry")
            seen.add(member.name)
            total += member.size
            install.require(total <= ARCHIVE_LIMIT, "unpacked installer archive exceeds limit")
    install.require(seen == ARCHIVE_FILES | {"kexec"}, "installer archive entry missing")
    return total


def dispatch(directory, phase, role, archive, archive_sha):
    install.require(ARCHIVE_SHA.fullmatch(archive_sha), "invalid installer archive digest")
    binding, key, _, ssh = install.bootstrap_authority(directory, phase, role)
    prefix = f"{phase}-{role}"
    receipt = directory / f"{prefix}-kexec-dispatched.json"
    install.require(not os.path.lexists(receipt), "kexec already dispatched")
    old_boot = stock_boot(ssh, key, directory)
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        install.require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= ARCHIVE_LIMIT
                        and hashlib.file_digest(source, "sha256").hexdigest() == archive_sha,
                        "installer archive rejected")
        source.seek(0)
        unpacked = archive_unpacked_bytes(source)
        source.seek(0)
        install.remote(ssh, install.remaining(directory, binding, 420), UPLOAD,
                       old_boot, archive_sha, str(info.st_size), str(unpacked), stdin=source)
    fresh, fresh_key, _, _ = install.bootstrap_authority(directory, phase, role)
    install.require((fresh, fresh_key) == (binding, key), "stock target changed before kexec")
    seconds = install.remaining(directory, binding, 120)
    install.write_private(receipt, {"binding": binding, "bootstrapKey": key,
                                   "stockBootId": old_boot, "archiveSha256": archive_sha,
                                   "archiveBytes": info.st_size, "archiveUnpackedBytes": unpacked})
    install.remote(ssh, seconds, KEXEC, old_boot)


def verify_ram(directory, phase, role):
    binding, key, _, ssh = install.bootstrap_authority(directory, phase, role)
    prefix = f"{phase}-{role}"
    sent, digest = install.read_json(directory / f"{prefix}-kexec-dispatched.json")
    install.require(sent["binding"] == binding and sent["bootstrapKey"] == key,
                    "RAM target does not match stock dispatch")
    boot = install.ram_boot_id(ssh)
    install.require(boot != sent["stockBootId"], "stock boot is still running")
    install.write_private(directory / f"{prefix}-ram-ready.json",
                          {"dispatchSha256": digest, "bootId": boot})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=install.PHASES)
    parser.add_argument("role", choices=install.ROLES)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dispatch", nargs=2, metavar=("ARCHIVE", "SHA256"))
    action.add_argument("--verify-ram", action="store_true")
    args = parser.parse_args()
    try:
        directory = install.private_directory(args.directory)
        if args.dispatch:
            dispatch(directory, args.phase, args.role, *args.dispatch)
            print("Pinned stock host accepted the verified RAM installer dispatch.")
        else:
            verify_ram(directory, args.phase, args.role)
            print("The pinned host now runs the RAM installer on a new boot.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        print("Stock kexec operation rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
