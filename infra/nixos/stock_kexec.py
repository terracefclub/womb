"""Enter and verify the RAM installer through an independently pinned stock host."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

import rescue_install as install


ARCHIVE_LIMIT = 1_073_741_824
ARCHIVE_SHA = re.compile(r"[a-f0-9]{64}")
ARCHIVE_FILES = {"kexec/bzImage", "kexec/initrd", "kexec/ip", "kexec/kexec", "kexec/run"}
SCRATCH = "/run/terrace-stock-kexec"
# Every check is its own simple command: under set -e a failure inside an && list
# neither exits nor fires the ERR trap. A failed check prints only its line number in
# this fixed script and its status, then stops; nothing the host holds is echoed.
# Dispatch reports that line, and the Python side still accepts only a boot ID.
STOCK_PROBE = """
set -euo pipefail
trap 'printf "stock-check-failed %d %d\\n" "$LINENO" "$?"; exit 0' ERR
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
# The install layout is UEFI only. A kernel locked down by Secure Boot would refuse
# the unsigned kexec, so require no lockdown wherever the kernel reports it.
test -d /sys/firmware/efi
if test -e /sys/kernel/security/lockdown; then
  grep -q '\\[none\\]' /sys/kernel/security/lockdown
fi
grep -qx 'ID=debian' /etc/os-release
grep -Eq '^VERSION_ID="?13"?$' /etc/os-release
test "$(findmnt -rn -o FSTYPE --mountpoint /run)" = tmpfs
# Allowance: cloud-init exits 2 when it finished with recoverable errors only, such as
# warnings a provider datasource or vendor data raises. Accept exactly that state:
# done, degraded, no errors and no failed module. Trust-relevant effects are checked below.
cloud_init=0
cloud-init status --wait >/dev/null || cloud_init=$?
if test "$cloud_init" != 0; then
  test "$cloud_init" = 2
  cloud_status="$(cloud-init status --format json)" || test "$?" = 2
  python3 -I -c '
import json, sys
s = json.loads(sys.argv[1])
found = s.get("recoverable_errors")
sys.exit(not (s.get("status") == "done" and s.get("extended_status") == "degraded done"
              and s.get("errors") == [] and isinstance(found, dict)
              and all(isinstance(items, list) and all(isinstance(item, str) and not (
                  "Running module" in item and "failed" in item) for item in items)
                      for items in found.values())))
' "$cloud_status"
fi
test -n "${SSH_CONNECTION:-}"
read -r client _ laddr lport <<<"$SSH_CONNECTION"
# sshd -T resolves relative paths against its cwd; the daemon runs from /.
cd /
settings="$(/usr/sbin/sshd -T -C "user=root,host=$client,addr=$client,laddr=$laddr,lport=$lport")"
grep -qx 'pubkeyauthentication yes' <<<"$settings"
grep -qx 'authenticationmethods publickey' <<<"$settings"
grep -qx 'allowusers root' <<<"$settings"
grep -qx 'passwordauthentication no' <<<"$settings"
grep -qx 'kbdinteractiveauthentication no' <<<"$settings"
grep -qx 'hostbasedauthentication no' <<<"$settings"
grep -qx 'gssapiauthentication no' <<<"$settings"
grep -Eq '^permitrootlogin (prohibit-password|without-password)$' <<<"$settings"
# No login route besides the authorized_keys files checked below.
grep -Eqx 'authorizedkeysfile \\.ssh/authorized_keys( \\.ssh/authorized_keys2)?' <<<"$settings"
grep -qx 'authorizedkeyscommand none' <<<"$settings"
grep -qx 'authorizedprincipalscommand none' <<<"$settings"
grep -qx 'authorizedprincipalsfile none' <<<"$settings"
grep -qx 'trustedusercakeys none' <<<"$settings"
grep -qx 'hostkeyagent none' <<<"$settings"
if grep -q '^hostcertificate ' <<<"$settings"; then
  false
fi
hostkeys="$(grep '^hostkey ' <<<"$settings")"
grep -qx 'hostkey /etc/ssh/ssh_host_ed25519_key' <<<"$hostkeys"
# Allowance: a provider drop-in may name other HostKey files. sshd serves only keys
# that exist, so each other path must be plain, absolute and absent; the key files
# are counted below. The raw read keeps any whitespace that would hide a path.
while IFS= read -r line; do
  path="${line#hostkey }"
  if test "$path" != /etc/ssh/ssh_host_ed25519_key; then
    [[ $path =~ ^/[A-Za-z0-9_./-]+$ ]]
    test ! -e "$path"
    test ! -L "$path"
  fi
done <<<"$hostkeys"
test -x /usr/bin/ip
test "$(stat -Lc '%u:%g:%a' /usr/bin/ip)" = 0:0:755
/usr/bin/ip --json addr >/dev/null
# The upstream kexec runner packs the carried state with cpio and gzip.
command -v cpio >/dev/null
command -v gzip >/dev/null
keys=(/etc/ssh/ssh_host_*)
test "${#keys[@]}" = 2
test "${keys[0]}" = /etc/ssh/ssh_host_ed25519_key
test "${keys[1]}" = /etc/ssh/ssh_host_ed25519_key.pub
for key in "${keys[@]}"; do
  test -f "$key"
  test ! -L "$key"
done
test "$(stat -c '%u:%a:%h' "${keys[0]}")" = 0:600:1
read -r kind key _ < <(ssh-keygen -y -f "${keys[0]}")
test "$kind $key" = "ssh-ed25519 $1"
mapfile -t -n 2 published <"${keys[1]}"
test "${#published[@]}" = 1
# The host generated this key itself, so ssh-keygen appended a root@host comment.
read -r kind key _ <<<"${published[0]}"
test "$kind $key" = "ssh-ed25519 $1"
auth=/root/.ssh/authorized_keys
test -f "$auth"
test ! -L "$auth"
test "$(stat -c '%u:%h' "$auth")" = 0:1
test ! -e /root/.ssh/authorized_keys2
test ! -L /root/.ssh/authorized_keys2
test ! -e /etc/ssh/authorized_keys.d/root
test ! -L /etc/ssh/authorized_keys.d/root
# Allowance: provider metadata can add the operator key a second time. Accept up to four
# lines that are all that one key: no other key, no options and no other kind of line.
mapfile -t -n 5 lines < "$auth"
test "${#lines[@]}" -ge 1
test "${#lines[@]}" -le 4
for line in "${lines[@]}"; do
  read -r kind key _ <<<"$line"
  test "$kind $key" = "ssh-ed25519 $2"
done
cat /proc/sys/kernel/random/boot_id
"""
STOCK_FAILURE = re.compile(rb"stock-check-failed ([1-9][0-9]{0,2}) ([1-9][0-9]{0,2})\n")
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
test -x kexec/run
test -f kexec/run
test ! -L kexec/run
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


class StockCheckFailed(install.Rejected):
    """A stock host check failed; only its line in STOCK_PROBE and its status are kept."""

    def __init__(self, line, status):
        super().__init__("stock host check failed")
        self.line, self.status = line, status


def stock_check(ssh, key, operator):
    output = install.remote(ssh, 60, STOCK_PROBE, key, operator, capture=True)
    failure = STOCK_FAILURE.fullmatch(output)
    if failure:
        raise StockCheckFailed(*map(int, failure.groups()))
    return boot_id(output)


def stock_boot(ssh, key, directory):
    public = install.checked_process(
        ["ssh-keygen", "-y", "-f", str(directory / "operator-ed25519")], 5, capture=True
    ).decode("ascii").split()
    install.require(len(public) >= 2 and public[0] == "ssh-ed25519", "invalid operator key")
    return stock_check(ssh, key, public[1])


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


# The runner kexecs 6 s after dispatch returns, then the kernel unpacks the xz initrd
# that holds the whole RAM system. The native fixture uses this same wait.
RAM_WAIT_SECONDS = 150
RAM_RETRY_DELAY = 2
RAM_ATTEMPT_SECONDS = 35
RAM_TIMELINE_LIMIT = 16
# Each attempt is reduced to one fixed state; ssh's own messages are never kept.
# host-key and denied are final, so they count only as ssh's own whole lines. A peer can
# echo text inside ssh's "Bad remote protocol version identification" line before key
# exchange, but it cannot produce a separate line, so it can only delay, never stop, the wait.
RAM_FINAL_LINES = (
    ("host-key", re.compile(rb"Host key verification failed[.]")),
    ("denied", re.compile(rb"root@[0-9.]{7,15}: Permission denied [(][a-z,-]{1,64}[)][.]")),
)
RAM_SSH_STATES = (
    ("refused", (b"connection refused",)),
    # Connected, but sshd sent no identification in time.
    ("no-banner", (b"timed out during banner exchange",)),
    ("no-answer", (b"timed out", b"no route to host", b"network is unreachable",
                   b"host is unreachable")),
    ("closed", (b"kex_exchange_identification", b"connection closed", b"connection reset",
                b"broken pipe")),
)
RAM_FINAL_STATES = {state for state, _ in RAM_FINAL_LINES}
RAM_STATES = (RAM_FINAL_STATES | {state for state, _ in RAM_SSH_STATES}
              | {"stalled", "ssh-other", "not-ram", "stock-boot", "ready"})
RAM_OUTPUT_LIMIT = 4096
# ssh's own ConnectTimeout is 10 s: an attempt that runs longer had connected.
RAM_CONNECT_SECONDS = 11
# Boot timing from the RAM installer's own clock, for telling a slow boot from a stuck one:
# EFI visibility, uptime, and when the initrd, the switched root and sshd started.
RAM_BOOT_FACTS = """
set -euo pipefail
if test -d /sys/firmware/efi; then efi=1; else efi=0; fi
read -r uptime _ </proc/uptime
initrd="$(systemctl show --property=InitRDTimestampMonotonic --value)"
userspace="$(systemctl show --property=UserspaceTimestampMonotonic --value)"
sshd="$(systemctl show sshd.service --property=ActiveEnterTimestampMonotonic --value)"
printf '%s %s %s %s %s\n' "$efi" "$uptime" "$initrd" "$userspace" "$sshd"
"""
RAM_BOOT_LINE = re.compile(r"([01]) ([0-9]{1,9})[.][0-9]{1,3} ([0-9]{1,15}) ([0-9]{1,15}) ([0-9]{1,15})")


class RamNotReady(install.Rejected):
    """The RAM installer did not answer in time; only fixed states and seconds are kept."""

    def __init__(self, seconds, timeline):
        super().__init__("RAM installer not ready")
        self.seconds, self.timeline = seconds, timeline

    def report(self):
        observed = " ".join(f"{state}@{second:d}" for state, second in self.timeline)
        return (f"RAM installer not ready after {self.seconds:d} s; observed {observed}. "
                "Private inputs suppressed.")


def ssh_state(code, stderr):
    if code == 0:
        return "ready"
    if code < 0:
        return "ssh-other"
    if code != 255:
        return "not-ram"
    lines = stderr.splitlines()
    for state, line in RAM_FINAL_LINES:
        if any(line.fullmatch(item.strip()) for item in lines):
            return state
    lower = stderr.lower()
    for state, markers in RAM_SSH_STATES:
        if any(marker in lower for marker in markers):
            return state
    return "ssh-other"


def bounded_run(argv, seconds):
    """Run argv with at most RAM_OUTPUT_LIMIT bytes per stream.

    Returns ("done", code, stdout, stderr), ("timeout", elapsed) or ("overflow",)."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        start = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - start >= seconds:
                    return "timeout", time.monotonic() - start
                if max(os.fstat(stdout.fileno()).st_size,
                       os.fstat(stderr.fileno()).st_size) > RAM_OUTPUT_LIMIT:
                    return ("overflow",)
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        streams = []
        for stream in (stdout, stderr):
            stream.seek(0)
            streams.append(stream.read(RAM_OUTPUT_LIMIT + 1))
        if max(map(len, streams)) > RAM_OUTPUT_LIMIT:
            return ("overflow",)
        return "done", process.returncode, *streams


def ram_attempt(ssh, seconds):
    """Run the RAM probe once; return a fixed state and, when ready, the boot ID."""
    command = shlex.join(["timeout", "--kill-after=5", "20", "bash", "-c",
                          "set -euo pipefail\n" + install.RAM_PROBE, "terrace"])
    result = bounded_run([*ssh, command], seconds)
    if result[0] == "overflow":
        return "ssh-other", None
    if result[0] == "timeout":
        # Past ssh's ConnectTimeout the attempt had connected and then stalled.
        return ("stalled" if result[1] >= RAM_CONNECT_SECONDS else "no-answer"), None
    _, code, stdout, stderr = result
    state = ssh_state(code, stderr)
    if state != "ready":
        return state, None
    boot = stdout.decode("ascii", "replace").strip()
    if len(stdout) > 128 or not install.BOOT_ID.fullmatch(boot):
        return "not-ram", None
    return "ready", boot


def ram_boot_facts(ssh):
    """Best effort: a diagnostic, never a gate."""
    try:
        output = install.remote(ssh, 20, RAM_BOOT_FACTS, capture=True).decode("ascii").strip()
    except (install.Rejected, OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    match = RAM_BOOT_LINE.fullmatch(output)
    if not match:
        return None
    efi, uptime, initrd, userspace, sshd = match.groups()
    return {"efi": efi == "1", "uptimeSeconds": int(uptime), "initrdStartMs": int(initrd) // 1000,
            "userspaceStartMs": int(userspace) // 1000, "sshdActiveMs": int(sshd) // 1000}


def compact_timeline(timeline):
    if len(timeline) <= RAM_TIMELINE_LIMIT:
        return list(timeline)
    return timeline[:RAM_TIMELINE_LIMIT - 1] + timeline[-1:]


def verify_ram(directory, phase, role):
    binding, key, _, ssh = install.bootstrap_authority(directory, phase, role)
    prefix = f"{phase}-{role}"
    sent, digest = install.read_json(directory / f"{prefix}-kexec-dispatched.json")
    install.require(sent["binding"] == binding and sent["bootstrapKey"] == key,
                    "RAM target does not match stock dispatch")
    start = time.monotonic()
    deadline = start + install.remaining(directory, binding, RAM_WAIT_SECONDS)
    timeline = []
    while True:
        second = int(time.monotonic() - start)
        budget = max(1, min(RAM_ATTEMPT_SECONDS, int(deadline - time.monotonic())))
        state, boot = ram_attempt(ssh, budget)
        if state == "ready" and boot == sent["stockBootId"]:
            state = "stock-boot"
        install.require(state in RAM_STATES, "unknown RAM wait state")
        if not timeline or timeline[-1][0] != state:
            timeline.append([state, second])
        if state == "ready":
            break
        if state in RAM_FINAL_STATES or time.monotonic() + RAM_RETRY_DELAY >= deadline:
            raise RamNotReady(int(time.monotonic() - start), compact_timeline(timeline))
        time.sleep(RAM_RETRY_DELAY)
    # The wait can outlast one provider binding; the server must still be the dispatched one.
    fresh, fresh_key, _, _ = install.bootstrap_authority(directory, phase, role)
    install.require((fresh, fresh_key) == (binding, key), "RAM target changed while waiting")
    install.write_private(directory / f"{prefix}-ram-ready.json",
                          {"dispatchSha256": digest, "bootId": boot, "waitSeconds": second,
                           "timeline": compact_timeline(timeline), "boot": ram_boot_facts(ssh)})


# Install target checks on the RAM installer, before its whole-disk receipt is written.
# A failure keeps one fixed label; the facts script adds only its line and status.
TARGET_FACTS = """
set -euo pipefail
trap 'printf "target-facts-failed %d %d\\n" "$LINENO" "$?"; exit 0' ERR
test "$(cat /etc/terrace-ram-installer)" = terrace-ram-installer-v1
virt="$(systemd-detect-virt --vm)" || test "$?" = 1
printf '%s\\n' "$virt"
route="$(ip -o -4 route get 1.1.1.1)"
printf '%s\\n' "$route"
lsblk --bytes --json --paths --nodeps --output PATH,TYPE,SIZE,RO,RM,MOUNTPOINTS
"""
TARGET_FAILURE = re.compile(rb"target-facts-failed ([1-9][0-9]{0,2}) ([1-9][0-9]{0,2})\n")
TARGET_LABELS = (
    "ram-authority", "target-facts", "virtualization", "route-source", "single-disk", "disk-name",
    "disk-state", "disk-size", "whole-disk-probe", "probe-boot-id", "probe-alias", "probe-whole-disk",
    "probe-identity", "probe-partition-state", "probe-holders", "disk-receipt",
)
# parse_probe's fixed messages, as labels.
PROBE_LABELS = {
    "invalid Rescue boot identity": "probe-boot-id",
    "disk alias did not resolve to an allowed whole disk": "probe-alias",
    "probe did not resolve one whole disk": "probe-whole-disk",
    "invalid disk identity": "probe-identity",
    "disk is read-only, removable or mounted": "probe-partition-state",
    "disk has holders": "probe-holders",
}
MIB = 1024 ** 2
GIB = 1024 ** 3


class TargetCheckFailed(install.Rejected):
    """An install target check failed; only a fixed label and script line and status are kept."""

    def __init__(self, label, line=None, status=None):
        super().__init__("install target check failed")
        install.require(label in TARGET_LABELS, "unknown install target label")
        self.label, self.line, self.status = label, line, status

    def report(self):
        where = "" if self.line is None else f" line {self.line:d} status {self.status:d}"
        return f"Install target check failed: {self.label}{where}. Private inputs suppressed."


def disk_size_fits(size, capacity_gb):
    # A provider's "N GB" disk is N decimal gigabytes or N GiB; allow either, with 4 MiB slack.
    return type(size) is int and capacity_gb * 10 ** 9 - 4 * MIB <= size <= capacity_gb * GIB + 4 * MIB


def check_target(output, ipv4, capacity_gb):
    """Check the RAM host's target facts; return its single whole disk."""
    if failure := TARGET_FAILURE.fullmatch(output):
        raise TargetCheckFailed("target-facts", *map(int, failure.groups()))
    try:
        virt, route, raw = output.decode("ascii").split("\n", 2)
        devices = json.loads(raw)["blockdevices"]
        install.require(isinstance(devices, list) and all(isinstance(row, dict) for row in devices),
                        "invalid block device facts")
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise TargetCheckFailed("target-facts") from None
    if virt not in ("kvm", "qemu"):
        raise TargetCheckFailed("virtualization")
    items = route.split()
    if "src" not in items[:-1] or items[items.index("src") + 1] != ipv4:
        raise TargetCheckFailed("route-source")
    disks = [row for row in devices if row.get("type") == "disk"]
    if len(disks) != 1:
        raise TargetCheckFailed("single-disk")
    disk = disks[0]
    if not install.valid_disk(disk.get("path")) or not install.DISK.fullmatch(disk["path"]):
        raise TargetCheckFailed("disk-name")
    if disk.get("ro") is not False or disk.get("rm") is not False or disk.get("mountpoints") != [None]:
        raise TargetCheckFailed("disk-state")
    if not disk_size_fits(disk.get("size"), capacity_gb):
        raise TargetCheckFailed("disk-size")
    return {"device": disk["path"], "size": disk["size"]}


def ram_target_check(ssh, ipv4, capacity_gb):
    install.require(type(capacity_gb) is int and 10 <= capacity_gb <= 4000, "invalid disk capacity")
    try:
        output = install.remote(ssh, 20, TARGET_FACTS, capture=True)
    except install.Rejected:
        raise TargetCheckFailed("target-facts") from None
    return check_target(output, ipv4, capacity_gb)


def inspect_ram_target(directory, phase, role, capacity_gb):
    """Check the kexec'd RAM installer as an install target, then record its whole-disk probe."""
    prefix = f"{phase}-{role}"
    try:
        binding, key, _, ssh = install.authority(directory, phase, role, ram=True)
    except (install.Rejected, OSError, KeyError, TypeError, subprocess.SubprocessError):
        raise TargetCheckFailed("ram-authority") from None
    target = ram_target_check(ssh, binding["ipv4"], capacity_gb)
    try:
        install.inspect(directory, phase, role, target["device"])
    except install.ProbeFailed as failure:
        raise TargetCheckFailed("whole-disk-probe", failure.line, failure.status) from None
    except install.Rejected as failure:
        raise TargetCheckFailed(PROBE_LABELS.get(str(failure), "whole-disk-probe")) from None
    except (OSError, KeyError, TypeError, subprocess.SubprocessError):
        raise TargetCheckFailed("whole-disk-probe") from None
    reviewed, digest = install.read_json(directory / f"{prefix}-disk.json")
    if (reviewed["binding"] != binding or reviewed["rescueKey"] != key
            or reviewed["disk"]["device"] != target["device"] or reviewed["disk"]["size"] != target["size"]):
        raise TargetCheckFailed("disk-receipt")
    return binding, key, reviewed["disk"], digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=install.PHASES)
    parser.add_argument("role", choices=install.ROLES)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--pin-proof", action="store_true")
    action.add_argument("--dispatch", nargs=2, metavar=("ARCHIVE", "SHA256"))
    action.add_argument("--verify-ram", action="store_true")
    action.add_argument("--inspect-target", type=int, metavar="DISK_GB")
    args = parser.parse_args()
    try:
        directory = install.private_directory(args.directory)
        if args.pin_proof:
            install.prove_host_key(directory, args.phase, args.role)
            print("Self-generated stock host key pinned from its keyed proof.")
        elif args.dispatch:
            dispatch(directory, args.phase, args.role, *args.dispatch)
            print("Pinned stock host accepted the verified RAM installer dispatch.")
        elif args.verify_ram:
            verify_ram(directory, args.phase, args.role)
            print("The pinned host now runs the RAM installer on a new boot.")
        else:
            inspect_ram_target(directory, args.phase, args.role, args.inspect_target)
            print("RAM install target checked and its whole disk recorded.")
    except (RamNotReady, TargetCheckFailed) as failure:
        # Fixed state names and integer seconds only; ssh's own messages are never shown.
        print(failure.report(), file=sys.stderr)
        return 1
    except StockCheckFailed as failure:
        # Both values are integers about this fixed script, never host or private data.
        print(f"Stock host check failed at line {failure.line:d} (status {failure.status:d}). "
              "Private inputs suppressed.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        print("Stock kexec operation rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
