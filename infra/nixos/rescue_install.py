"""Install reviewed closures on one independently authenticated, leased Rescue host."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
from rehearsal_lease import (  # noqa: E402
    PHASES, ROLES, Rejected, expected_names, positive_id, private_directory,
    read_private, write_private,
)


NIX_SHA = "0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158"
NIX_STORE = "/nix/store/irfrbndi76zhkvqsfhmsn4a99iafck29-nix-2.35.2/bin/nix-store"
NIX_SERVE = "/run/terrace-rescue-nix-serve"
TARGET_COPY_SECONDS = {"app": 1800, "relay": 420}
MAX_FACTS = 65536
DISK = re.compile(r"/dev/(?:sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)")
BY_ID = re.compile(r"/dev/disk/by-id/[A-Za-z0-9][A-Za-z0-9_.:+-]{0,220}")
STORE = re.compile(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+_.-]+")
KEY_PREFIX = b"\0\0\0\x0bssh-ed25519\0\0\0\x20"
BINDING_FIELDS = {"run", "phase", "role", "serverId", "ipv4", "expires", "observedAt"}


def require(condition, message):
    if not condition:
        raise Rejected(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_pairs(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), "duplicate JSON field")
    return result


def read_json(path, limit=MAX_FACTS):
    data = read_private(path, limit)
    return json.loads(data, object_pairs_hook=unique_pairs), sha(data)


def valid_disk(device):
    return isinstance(device, str) and bool(
        DISK.fullmatch(device)
        or (BY_ID.fullmatch(device) and re.search(r"-part[0-9]", device) is None)
    )


def latest_binding(directory, prefix):
    pattern = re.compile(re.escape(prefix) + r"-binding-([1-9][0-9]{0,19})[.]json")
    receipts = []
    for path in directory.glob(prefix + "-binding-*.json"):
        if match := pattern.fullmatch(path.name):
            receipts.append((int(match[1]), path))
    require(receipts, "no exact provider binding receipt")
    return read_json(max(receipts)[1])[0]


def require_open(directory, run, expires):
    manifest, _ = read_json(directory / "lease.json", 4 * 1024 * 1024)
    require(not os.path.lexists(directory / "closed.json") and manifest["schema"] == 1
            and manifest["run"] == run and time.time() < expires <= manifest["expires"],
            "install lease is closed, changed or expired")


def lease_binding(directory, phase, role, now):
    require(phase in PHASES and role in ROLES, "unknown install role or phase")
    manifest, _ = read_json(directory / "lease.json", 4 * 1024 * 1024)
    require(not os.path.lexists(directory / "closed.json"), "install lease closed")
    created, _ = read_json(directory / f"{phase}-created.json")
    prefix = f"{phase}-{role}"
    binding = latest_binding(directory, prefix)
    expected_names(manifest["run"])
    require(set(binding) == BINDING_FIELDS and manifest["schema"] == 1,
            "unexpected install receipt")
    require((binding["run"], binding["phase"], binding["role"]) == (manifest["run"], phase, role),
            "install receipt is for a different role or run")
    server_id = positive_id(binding["serverId"])
    require(server_id in created["servers"] and str(server_id) not in manifest["baseline"]["servers"],
            "server is not an owned created identity")
    require(type(binding["observedAt"]) is int and type(binding["expires"]) is int
            and 0 <= now - binding["observedAt"] <= 120
            and now < binding["expires"] <= manifest["expires"],
            "provider binding expired or stale")
    address = ipaddress.IPv4Address(binding["ipv4"])
    require(str(address) == binding["ipv4"] and address.is_global, "unexpected SSH address")
    return {key: value for key, value in binding.items() if key != "observedAt"}


def known_key(data, ipv4, expected):
    host, kind, encoded = data.decode("ascii").strip().split()
    require(host == ipv4 and kind == "ssh-ed25519", "unexpected pinned host key")
    blob = base64.b64decode(encoded, validate=True)
    require(blob.startswith(KEY_PREFIX) and len(blob) == len(KEY_PREFIX) + 32,
            "invalid Ed25519 host key")
    fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
    require(fingerprint == expected.strip(), "Rescue key differs from console fingerprint")
    return encoded


def ssh_options(directory, prefix):
    key = directory / "operator-ed25519"
    info = key.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            "operator key is not private")
    return [
        "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={directory / (prefix + '-rescue-known-hosts')}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519",
        "-o", "UpdateHostKeys=no", "-o", "ControlMaster=no", "-o", "ConnectTimeout=10",
    ]


def checked_process(argv, seconds, *, stdin=None, capture=False, env=None):
    # All child diagnostics are suppressed, including remote shell output.
    with tempfile.TemporaryFile() as output:
        stopping = False

        def stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            process = subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                       stdout=output if capture else subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, env=env)
            deadline = time.monotonic() + seconds
            try:
                while not os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    require(not stopping and time.monotonic() < deadline, "install command stopped")
                    if capture:
                        require(os.fstat(output.fileno()).st_size <= MAX_FACTS, "probe output exceeds limit")
                    time.sleep(0.05)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    process.wait(timeout=5)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        require(not stopping and process.returncode == 0, "install command rejected")
        output.seek(0)
        data = output.read(MAX_FACTS + 1)
        require(len(data) <= MAX_FACTS, "probe output exceeds limit")
        return data


def remote(ssh, seconds, script, *args, stdin=None, capture=False):
    command = shlex.join(["timeout", "--kill-after=10", str(seconds), "bash", "-c", script, "terrace", *args])
    return checked_process([*ssh, command], seconds + 20, stdin=stdin, capture=capture)


PROBE = """
set -euo pipefail
test "$(id -u)" = 0 && test "$(uname -m)" = x86_64
test ! -e /sys/firmware/efi
case "$(findmnt -rn -o FSTYPE /)" in tmpfs|ramfs|overlay|rootfs|squashfs) ;; *) exit 1;; esac
if findmnt -rn --mountpoint /mnt >/dev/null; then exit 1; else test "$?" = 1; fi
dev="$(readlink -e "$1")"
test -b "$dev"
cat /proc/sys/kernel/random/boot_id
printf '%s\n' "$dev"
exec lsblk --bytes --json --paths --output PATH,TYPE,SIZE,RO,RM,MOUNTPOINTS,MAJ:MIN -- "$dev"
"""

BOOTSTRAP = f"""
set -euo pipefail
umask 077
case "$1" in 430|1810) ;; *) exit 1;; esac
test ! -e /nix && test ! -L /nix
test ! -e {NIX_SERVE} && test ! -L {NIX_SERVE}
scratch="$(mktemp -d /tmp/terrace-rescue-nix.XXXXXXXX)"
trap 'rm -rf -- "$scratch"' EXIT
cat >"$scratch/nix.tar.xz"
printf '%s  %s\n' '{NIX_SHA}' "$scratch/nix.tar.xz" | sha256sum --check --status
tar -xJf "$scratch/nix.tar.xz" -C "$scratch"
payload="$scratch/nix-2.35.2-x86_64-linux"
mkdir -m 0755 /nix /nix/store
cp -a "$payload/store/." /nix/store/
{NIX_STORE} --option build-users-group "" --load-db <"$payload/.reginfo"
set -o noclobber
printf '#!/bin/sh\nexec timeout --kill-after=10 %s {NIX_STORE} --option build-users-group "" "$@"\n' "$1" >{NIX_SERVE}
chmod 0700 {NIX_SERVE}
"""

DISK_GUARD = """
test "$(cat /proc/sys/kernel/random/boot_id)" = "$1"
test "$(readlink -e "$2")" = "$3"
test "$(lsblk -dn -o MAJ:MIN -- "$3")" = "$4"
test "$(blockdev --getsize64 "$3")" = "$5"
"""

MOUNT_GUARD = """
mounted="$(findmnt -rn -o SOURCE,FSTYPE --mountpoint /mnt)"
read -r source kind <<<"$mounted"
test "$kind" = ext4
test "$(lsblk -dn -o PKNAME -- "$source")" = "${3##*/}"
"""

FORMAT = "set -euo pipefail\n" + DISK_GUARD + """
if findmnt -rn --mountpoint /mnt >/dev/null; then exit 1; else test "$?" = 1; fi
"$6"
""" + MOUNT_GUARD

FINISH = "set -euo pipefail\n" + DISK_GUARD + MOUNT_GUARD + """
"$6/bin/nixos-install" --root /mnt --system "$7" --no-root-passwd --no-channel-copy
umask 077
install -d -m 0755 /mnt/etc/ssh
test ! -L /mnt/etc/ssh
key=/mnt/etc/ssh/ssh_host_ed25519_key
test ! -e "$key" && test ! -L "$key"
set -o noclobber
cat >"$key"
test "$(ssh-keygen -y -f "$key" | cut -d ' ' -f 2)" = "$8"
sync -f /mnt
"""


def parse_probe(data, device):
    boot, canonical, inventory = data.decode("utf-8").split("\n", 2)
    require(bool(re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot)),
            "invalid Rescue boot identity")
    require(DISK.fullmatch(canonical) and (device == canonical or BY_ID.fullmatch(device)),
            "disk alias did not resolve to an allowed whole disk")
    disks = json.loads(inventory, object_pairs_hook=unique_pairs)["blockdevices"]
    require(len(disks) == 1 and disks[0]["path"] == canonical and disks[0]["type"] == "disk",
            "probe did not resolve one whole disk")
    disk = disks[0]
    require(type(disk["size"]) is int and disk["size"] > 0
            and re.fullmatch(r"[0-9]+:[0-9]+", disk["maj:min"]),
            "invalid disk identity")
    for row in [disk, *disk.get("children", [])]:
        require(row["ro"] is False and row["rm"] is False and row["mountpoints"] == [None],
                "disk is read-only, removable or mounted")
        if row is not disk:
            require(row["type"] == "part" and not row.get("children"), "disk has holders")
    return {"device": device, "canonical": canonical, "size": disk["size"],
            "majorMinor": disk["maj:min"], "bootId": boot}


def authority(directory, phase, role):
    binding = lease_binding(directory, phase, role, time.time())
    prefix = f"{phase}-{role}"
    ipv4 = binding["ipv4"]
    key = known_key(read_private(directory / f"{prefix}-rescue-known-hosts", 4096), ipv4,
                    read_private(directory / f"{prefix}-rescue-fingerprint", 128))
    options = ssh_options(directory, prefix)
    return binding, key, options, ["ssh", *options, "root@" + ipv4]


def inspect(directory, phase, role, device):
    require(valid_disk(device), "invalid whole-disk syntax")
    binding, key, _, ssh = authority(directory, phase, role)
    disk = parse_probe(remote(ssh, 20, PROBE, device, capture=True), device)
    write_private(directory / f"{phase}-{role}-disk.json",
                  {"binding": binding, "rescueKey": key, "disk": disk})


def checked_plan(directory, prefix, approved, disk_digest):
    plan, digest = read_json(directory / f"{prefix}-install.json")
    require(digest == approved and set(plan) == {"diskSha256", "disko", "installer", "system"}
            and plan["diskSha256"] == disk_digest, "reviewed install plan changed")
    for name in ("disko", "installer", "system"):
        path = plan[name]
        require(isinstance(path, str) and STORE.fullmatch(path)
                and Path(path).exists(), "missing reviewed Nix store output")
    return plan


def copy_store(binding, options, seconds, paths, *, target=False):
    parameters = {"remote-program": NIX_SERVE}
    if target:
        parameters["remote-store"] = "local?root=/mnt"
    destination = "ssh://root@" + binding["ipv4"] + "?" + urlencode(parameters)
    # Nix passes this exact option set to OpenSSH. Only our fixed safe path
    # alphabet can occur in the key and known_hosts arguments.
    env = dict(os.environ, NIX_SSHOPTS=shlex.join(options))
    checked_process(["nix", "copy", "--to", destination, *paths], seconds, env=env)


def remaining(directory, binding, limit):
    require_open(directory, binding["run"], binding["expires"])
    seconds = min(limit, int(binding["expires"] - time.time()) - 20)
    require(seconds > 0, "install lease expired")
    return seconds


def install(directory, phase, role, approved, archive):
    prefix = f"{phase}-{role}"
    target_seconds = TARGET_COPY_SECONDS[role]
    require(re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(directory)), "unsupported SSH state path")
    binding, key, options, ssh = authority(directory, phase, role)
    reviewed, disk_digest = read_json(directory / f"{prefix}-disk.json")
    require(reviewed["binding"] == binding and reviewed["rescueKey"] == key,
            "install target changed after review")
    disk = reviewed["disk"]
    plan = checked_plan(directory, prefix, approved, disk_digest)
    staged_key = directory / f"{prefix}-installed-ed25519"
    key_bytes = read_private(staged_key, 4096)
    public = checked_process(["ssh-keygen", "-y", "-f", str(staged_key)], 5, capture=True)
    public_kind, public_data, *_ = public.decode("ascii").split()
    require(public_kind == "ssh-ed25519" and public_data != key, "installed host key is not distinct")
    installed_hosts = directory / f"{prefix}-installed-known-hosts"
    require(read_private(installed_hosts, 4096).split() ==
            [binding["ipv4"].encode(), b"ssh-ed25519", public_data.encode()],
            "installed host key does not match its independent pin")

    device = disk["device"]
    require(valid_disk(device) and parse_probe(remote(ssh, 20, PROBE, device, capture=True), device) == disk,
            "Rescue disk identity changed")
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= 100_000_000
                and hashlib.file_digest(source, "sha256").hexdigest() == NIX_SHA,
                "Rescue Nix archive rejected")
        source.seek(0)
        remote(ssh, remaining(directory, binding, 120), BOOTSTRAP, str(target_seconds + 10), stdin=source)
    copy_store(binding, options, remaining(directory, binding, 240), [plan["disko"], plan["installer"]])
    # Provider controller may refresh observedAt. The immutable binding and
    # Rescue pin must still match, immediately before any disk write.
    fresh, fresh_key, _, _ = authority(directory, phase, role)
    require(fresh == binding and fresh_key == key, "install target changed before formatting")
    require(parse_probe(remote(ssh, 20, PROBE, device, capture=True), device) == disk,
            "Rescue disk identity changed before formatting")
    write_private(directory / f"{prefix}-install-dispatched.json", {
        "binding": binding, "diskSha256": disk_digest, "planSha256": approved,
    })
    disk_args = [disk["bootId"], device, disk["canonical"], disk["majorMinor"], str(disk["size"])]
    remote(ssh, remaining(directory, binding, 90), FORMAT, *disk_args, plan["disko"])
    copy_store(binding, options, remaining(directory, binding, target_seconds), [plan["system"]], target=True)
    with tempfile.TemporaryFile() as key_stream:
        key_stream.write(key_bytes)
        key_stream.seek(0)
        remote(ssh, remaining(directory, binding, 180), FINISH, *disk_args, plan["installer"],
               plan["system"], public_data, stdin=key_stream)
    write_private(directory / f"{prefix}-installed.json", {
        "binding": binding, "system": plan["system"], "planSha256": approved,
        "installedHostKey": public_data, "mountedExt4Verified": True,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("phase", choices=PHASES)
    parser.add_argument("role", choices=ROLES)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect-disk")
    action.add_argument("--install-approved-sha")
    parser.add_argument("--bootstrap-archive", type=Path)
    args = parser.parse_args()
    try:
        directory = private_directory(args.directory)
        if args.inspect_disk is not None:
            inspect(directory, args.phase, args.role, args.inspect_disk)
            message = "Pinned Rescue host and whole-disk probe recorded privately."
        else:
            require(args.bootstrap_archive is not None, "Rescue Nix archive is required")
            install(directory, args.phase, args.role, args.install_approved_sha, args.bootstrap_archive)
            message = "Reviewed NixOS installed to the verified disk. Reboot is separate."
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Rescue operation rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
