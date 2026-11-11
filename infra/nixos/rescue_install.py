"""Install reviewed closures on one independently authenticated, leased Rescue host."""

import argparse
from contextlib import contextmanager
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
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
    read_private, reviewed_probe_mode, write_private,
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
BOOT_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
PROOF_LINE = re.compile(rb"terrace-hostkey-proof v1 ([0-9a-f]{64})")
PROOF_OUTPUT_LIMIT = 65536
RAM_PROBE = """
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
test "$(cat /etc/terrace-ram-installer)" = terrace-ram-installer-v1
test "$(findmnt -rn -o FSTYPE --mountpoint /)" = tmpfs
test "$(findmnt -rn -o FSTYPE --mountpoint /nix/.ro-store)" = squashfs
test "$(findmnt -rn -o FSTYPE --mountpoint /nix/.rw-store)" = tmpfs
# NixOS remounts the store read-only over itself, so expect stacked overlays.
test "$(findmnt -rn -o FSTYPE --mountpoint /nix/store | sort -u)" = overlay
test -x /run/current-system/sw/bin/nix-store
cat /proc/sys/kernel/random/boot_id
"""


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


def ssh_options(directory, prefix, pin="rescue"):
    key = directory / "operator-ed25519"
    info = key.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
            "operator key is not private")
    return [
        "-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={directory / (prefix + '-' + pin + '-known-hosts')}",
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


# A failed check prints only its line and status, never host output (see parse_probe):
# facts are captured first and printed together at the end, so a failure emits the marker alone.
# Without a NAME column lsblk prints a flat list, so --tree keeps partitions under their disk.
PROBE = """
set -euo pipefail
trap 'printf "probe-failed %d %d\\n" "$LINENO" "$?"; exit 0' ERR
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
case "$(findmnt -rn -o FSTYPE /)" in tmpfs|ramfs|overlay|rootfs|squashfs) ;; *) false;; esac
if findmnt -rn --mountpoint /mnt >/dev/null; then false; else test "$?" = 1; fi
dev="$(readlink -e "$1")"
test -b "$dev"
boot="$(cat /proc/sys/kernel/random/boot_id)"
inventory="$(lsblk --tree --bytes --json --paths --output PATH,TYPE,SIZE,RO,RM,MOUNTPOINTS,MAJ:MIN -- "$dev")"
printf '%s\n%s\n%s\n' "$boot" "$dev" "$inventory"
"""
PROBE_FAILURE = re.compile(rb"probe-failed ([1-9][0-9]{0,2}) ([1-9][0-9]{0,2})\n")

BOOTSTRAP = f"""
set -euo pipefail
umask 077
case "$1" in 430|1810) ;; *) exit 1;; esac
test ! -e /nix
test ! -L /nix
test ! -e {NIX_SERVE}
test ! -L {NIX_SERVE}
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

RAM_SERVE = f"""
set -euo pipefail
umask 077
case "$1" in 430|1810) ;; *) exit 1;; esac
test -x /run/current-system/sw/bin/nix-store
set -o noclobber
printf '#!/bin/sh\nexec timeout --kill-after=10 %s /run/current-system/sw/bin/nix-store "$@"\n' "$1" >{NIX_SERVE}
chmod 0700 {NIX_SERVE}
"""

# Every lsblk and findmnt text comparison uses raw output (-r): the default table
# output pads columns, e.g. MAJ:MIN prints as "  8:0  " while lsblk JSON says "8:0".
DISK_GUARD = """
test "$(cat /proc/sys/kernel/random/boot_id)" = "$1"
test "$(readlink -e "$2")" = "$3"
test "$(lsblk -drn -o MAJ:MIN -- "$3")" = "$4"
test "$(blockdev --getsize64 "$3")" = "$5"
"""

MOUNT_GUARD = """
mounted="$(findmnt -rn -o SOURCE,FSTYPE --mountpoint /mnt)"
read -r source kind <<<"$mounted"
test "$kind" = ext4
test "$(lsblk -drn -o PKNAME --paths -- "$source")" = "$3"
esp="$(findmnt -rn -o SOURCE,FSTYPE --mountpoint /mnt/boot)"
read -r esp_source esp_kind <<<"$esp"
test "$esp_kind" = vfat
test "$(lsblk -drn -o PKNAME --paths -- "$esp_source")" = "$3"
udevadm settle --timeout=30
esp_type="$(lsblk -drn -o PARTTYPE -- "$esp_source")"
test "${esp_type,,}" = c12a7328-f81f-11d2-ba4b-00a0c93ec93b
"""


def step_script(name, body):
    """A disk-writing step that reports only a fixed failure line, like PROBE.

    All command output is discarded; fd 3 carries just "<name>-ok" or
    "<name>-failed LINE STATUS", so a failure names the line that stopped it.
    Long-running tools get fd 3 closed, so nothing they leave behind holds the verdict.
    """
    return ("set -euo pipefail\n"
            "exec 3>&1 >/dev/null 2>&1\n"
            f"trap 'printf \"{name}-failed %d %d\\n\" \"$LINENO\" \"$?\" >&3; exit 0' ERR\n"
            + body + f"printf '{name}-ok\\n' >&3\n")


FORMAT = step_script("format", DISK_GUARD + """
if findmnt -rn --mountpoint /mnt >/dev/null; then false; else test "$?" = 1; fi
"$6" 3>&-
""" + MOUNT_GUARD)

FINISH = step_script("finish", DISK_GUARD + MOUNT_GUARD + """
"$6/bin/nixos-install" --root /mnt --system "$7" --no-root-passwd --no-channel-copy 3>&-
# No firmware variables are written, so the disk must boot from the removable path.
test -f /mnt/boot/EFI/BOOT/BOOTX64.EFI
test ! -L /mnt/boot/EFI/BOOT/BOOTX64.EFI
test -f /mnt/boot/loader/loader.conf
umask 077
install -d -m 0755 /mnt/etc/ssh
test ! -L /mnt/etc/ssh
key=/mnt/etc/ssh/ssh_host_ed25519_key
test ! -e "$key"
test ! -L "$key"
set -o noclobber
cat >"$key"
test "$(ssh-keygen -y -f "$key" | cut -d ' ' -f 2)" = "$8"
sync -f /mnt
""")
STEP_FAILURE = re.compile(rb"(format|finish)-failed ([1-9][0-9]{0,2}) ([1-9][0-9]{0,2})\n")
INSTALL_LABELS = ("authority", "disk-receipt", "plan", "installed-key", "probe", "store-service",
                  "store-copy", "recheck", "format", "target-copy", "finish", "installed-receipt")


def unmounted(row):
    # lsblk prints an unmounted device's MOUNTPOINTS as [null] in older util-linux and
    # as [] in newer releases (Falcon's 2.42.3 does); anything else is a mount.
    return row.get("mountpoints") in ([], [None])


class ProbeFailed(Rejected):
    """A whole-disk probe check failed; only its line in PROBE and its status are kept."""

    def __init__(self, line, status):
        super().__init__("whole-disk probe check failed")
        self.line, self.status = line, status


class InstallFailed(Rejected):
    """An install stage failed; only its fixed label and, for a step script, its line are kept."""

    def __init__(self, label, line=None, status=None):
        super().__init__("install stage failed")
        assert label in INSTALL_LABELS
        self.label, self.line, self.status = label, line, status

    def report(self):
        where = f" line {self.line} status {self.status}" if self.line is not None else ""
        return f"Install failed at {self.label}{where}. Private inputs suppressed."


@contextmanager
def install_stage(label):
    try:
        yield
    except InstallFailed:
        raise
    except ProbeFailed as failure:
        raise InstallFailed(label, failure.line, failure.status) from None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise InstallFailed(label) from None


def run_step(ssh, seconds, name, script, *args, stdin=None):
    """Run FORMAT or FINISH; a failed check raises its label with the script line."""
    output = remote(ssh, seconds, script, *args, stdin=stdin, capture=True)
    if output == f"{name}-ok\n".encode():
        return
    failure = STEP_FAILURE.fullmatch(output)
    if failure and failure[1].decode() == name:
        raise InstallFailed(name, int(failure[2]), int(failure[3]))
    raise InstallFailed(name)


def parse_probe(data, device):
    if failure := PROBE_FAILURE.fullmatch(data):
        raise ProbeFailed(*map(int, failure.groups()))
    boot, canonical, inventory = data.decode("utf-8").split("\n", 2)
    require(BOOT_ID.fullmatch(boot),
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
        require(row["ro"] is False and row["rm"] is False and unmounted(row),
                "disk is read-only, removable or mounted")
        if row is not disk:
            require(row["type"] == "part" and not row.get("children"), "disk has holders")
    return {"device": device, "canonical": canonical, "size": disk["size"],
            "majorMinor": disk["maj:min"], "bootId": boot}


def reviewed_bootstrap(directory, phase, role):
    """Return the reviewed run and proof-nonce digest for a known-host bootstrap."""
    from rehearsal_lease import reviewed_phase_input

    config, _ = reviewed_phase_input(directory, phase)
    require("bootstrapHostKeys" not in config, "host private keys must not pass through user data")
    if "bootstrapProofs" not in config:
        return None
    require(config["image"] == "debian-13", "unexpected stock bootstrap image")
    proof = config["bootstrapProofs"][role]
    require(isinstance(proof, dict) and set(proof) == {"nonceSha256"}
            and re.fullmatch(r"[a-f0-9]{64}", proof["nonceSha256"]), "reviewed bootstrap proof rejected")
    return config["run"], proof["nonceSha256"]


def strong_nonce(text):
    """A 256-bit lowercase hex nonce that is not an obvious placeholder."""
    return bool(re.fullmatch(r"[a-f0-9]{64}", text)) and len(set(text)) >= 8


def write_proof_nonce(path):
    """Create one private CSPRNG proof nonce file; never reuse or hand-type one."""
    nonce = secrets.token_hex(32)
    require(strong_nonce(nonce), "generated nonce rejected")
    write_new_text(path, nonce + "\n")
    return hashlib.sha256((nonce + "\n").encode("ascii")).hexdigest()


def proof_mac(nonce, run, phase, role, public):
    """The MAC the first-boot script publishes for its self-generated host key."""
    message = "|".join(("terrace-hostkey-v1", run, phase, role, public)).encode("ascii")
    return hmac.new(bytes.fromhex(nonce), message, hashlib.sha256).hexdigest()


def ed25519_key(encoded):
    blob = base64.b64decode(encoded, validate=True)
    require(blob.startswith(KEY_PREFIX) and len(blob) == len(KEY_PREFIX) + 32, "invalid Ed25519 host key")
    return b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")


def presented_host_key(target, extra, seconds):
    """Return the host key and proof MACs observed in one unauthenticated SSH session.

    The client offers no authentication method and records the presented key only in
    a throwaway file. The banner arrives inside the same encrypted transport.
    """
    with tempfile.TemporaryDirectory(prefix="terrace-hostkey-proof-") as scratch:
        known = Path(scratch) / "known_hosts"
        known.touch(mode=0o600)
        argv = [
            "ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
            "-o", "HashKnownHosts=no", "-o", "ControlMaster=no", "-o", "ConnectTimeout=10",
            "-o", "PreferredAuthentications=publickey", "-o", "PubkeyAuthentication=no",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "GSSAPIAuthentication=no", "-o", "HostbasedAuthentication=no",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "ForwardAgent=no",
            "-o", "ClearAllForwardings=yes", "-o", "LogLevel=INFO", *extra, target, "true",
        ]
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=seconds, check=False)
        except subprocess.TimeoutExpired:
            return None, []
        recorded = known.read_bytes()
        lines = [line.split() for line in recorded.splitlines() if line.strip()]
    if len(result.stderr) > PROOF_OUTPUT_LIMIT or not lines:
        return None, []
    require(len(lines) == 1 and len(lines[0]) == 3 and lines[0][1] == b"ssh-ed25519",
            "unexpected presented host key")
    key = lines[0][2].decode("ascii")
    ed25519_key(key)
    return key, [match.decode("ascii") for match in PROOF_LINE.findall(result.stderr)]


def verified_host_key(nonce, context, target, extra, seconds):
    """Return the presented key only if the banner MAC proves it; None if not yet published."""
    key, macs = presented_host_key(target, extra, seconds)
    if key is None or not macs:
        return None
    expected = proof_mac(nonce, *context, "ssh-ed25519 " + key)
    require(len(macs) == 1 and hmac.compare_digest(macs[0], expected), "host key proof rejected")
    return key


def proof_nonce(directory, prefix, digest):
    nonce = read_private(directory / f"{prefix}-bootstrap-nonce", 65)
    require(sha(nonce) == digest and re.fullmatch(rb"[a-f0-9]{64}\n", nonce)
            and strong_nonce(nonce.decode("ascii").strip()),
            "bootstrap proof nonce differs from approved input")
    return nonce.decode("ascii").strip()


def write_new_text(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def prove_host_key(directory, phase, role, *, seconds=300, delay=5):
    """Pin the self-generated stock host key once, from its keyed banner proof."""
    reviewed = reviewed_bootstrap(directory, phase, role)
    require(reviewed is not None, "stock bootstrap was not reviewed")
    run, digest = reviewed
    prefix = f"{phase}-{role}"
    outputs = [directory / f"{prefix}-bootstrap-{name}" for name in ("known-hosts", "fingerprint", "proof.json")]
    require(not any(os.path.lexists(path) for path in outputs), "bootstrap host key already pinned")
    nonce = proof_nonce(directory, prefix, digest)
    deadline = time.monotonic() + seconds
    while True:
        binding = lease_binding(directory, phase, role, time.time())
        key = verified_host_key(nonce, (run, phase, role), "root@" + binding["ipv4"], [],
                                remaining(directory, binding, 20))
        if key is not None:
            break
        require(time.monotonic() + delay < deadline, "host key proof was not published in time")
        time.sleep(delay)
    # The provider must still bind the same server and address after the attempt.
    current = lease_binding(directory, phase, role, time.time())
    require((current["serverId"], current["ipv4"]) == (binding["serverId"], binding["ipv4"]),
            "provider binding changed during the host key proof")
    fingerprint = ed25519_key(key)
    write_private(outputs[2], {"run": run, "serverId": binding["serverId"], "ipv4": binding["ipv4"],
                               "publicKey": "ssh-ed25519 " + key, "nonceSha256": digest,
                               "fingerprint": fingerprint.decode("ascii")})
    write_new_text(outputs[0], f"{binding['ipv4']} ssh-ed25519 {key}\n")
    write_new_text(outputs[1], fingerprint.decode("ascii") + "\n")
    return key


def bootstrap_authority(directory, phase, role):
    reviewed = reviewed_bootstrap(directory, phase, role)
    require(reviewed is not None, "stock bootstrap was not reviewed")
    binding = lease_binding(directory, phase, role, time.time())
    prefix = f"{phase}-{role}"
    ipv4 = binding["ipv4"]
    key = known_key(read_private(directory / f"{prefix}-bootstrap-known-hosts", 4096), ipv4,
                    read_private(directory / f"{prefix}-bootstrap-fingerprint", 128))
    proof, _ = read_json(directory / f"{prefix}-bootstrap-proof.json")
    require(proof == {"run": reviewed[0], "serverId": binding["serverId"], "ipv4": ipv4,
                      "publicKey": "ssh-ed25519 " + key, "nonceSha256": reviewed[1],
                      "fingerprint": ed25519_key(key).decode("ascii")},
            "stock pin differs from the proven host key")
    options = ssh_options(directory, prefix, "bootstrap")
    return binding, key, options, ["ssh", *options, "root@" + ipv4]


def ram_boot_id(ssh):
    boot_id = remote(ssh, 20, "set -euo pipefail\n" + RAM_PROBE, capture=True).decode("ascii").strip()
    require(BOOT_ID.fullmatch(boot_id), "invalid RAM installer boot identity")
    return boot_id


def authority(directory, phase, role, *, ram=False):
    if ram:
        binding, key, options, ssh = bootstrap_authority(directory, phase, role)
        prefix = f"{phase}-{role}"
        dispatch, dispatch_sha = read_json(directory / f"{prefix}-kexec-dispatched.json")
        ready, _ = read_json(directory / f"{prefix}-ram-ready.json")
        require(dispatch["binding"] == binding and dispatch["bootstrapKey"] == key
                and ready["dispatchSha256"] == dispatch_sha
                and ready["bootId"] != dispatch["stockBootId"]
                and ready["bootId"] == ram_boot_id(ssh), "RAM installer changed after kexec")
        return binding, key, options, ssh

    binding = lease_binding(directory, phase, role, time.time())
    prefix = f"{phase}-{role}"
    ipv4 = binding["ipv4"]
    key = known_key(read_private(directory / f"{prefix}-rescue-known-hosts", 4096), ipv4,
                    read_private(directory / f"{prefix}-rescue-fingerprint", 128))
    options = ssh_options(directory, prefix)
    return binding, key, options, ["ssh", *options, "root@" + ipv4]


def inspect(directory, phase, role, device):
    require(valid_disk(device), "invalid whole-disk syntax")
    require(not reviewed_probe_mode(directory, phase), "probe cannot inspect an install disk")
    ram = reviewed_bootstrap(directory, phase, role) is not None
    binding, key, _, ssh = authority(directory, phase, role, ram=ram)
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
    with install_stage("authority"):
        require(not reviewed_probe_mode(directory, phase), "console probe cannot write a disk")
        ram = reviewed_bootstrap(directory, phase, role) is not None
        prefix = f"{phase}-{role}"
        target_seconds = TARGET_COPY_SECONDS[role]
        require(re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(directory)), "unsupported SSH state path")
        binding, key, options, ssh = authority(directory, phase, role, ram=ram)
    with install_stage("disk-receipt"):
        reviewed, disk_digest = read_json(directory / f"{prefix}-disk.json")
        require(reviewed["binding"] == binding and reviewed["rescueKey"] == key,
                "install target changed after review")
        disk = reviewed["disk"]
    with install_stage("plan"):
        plan = checked_plan(directory, prefix, approved, disk_digest)
    with install_stage("installed-key"):
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
    with install_stage("probe"):
        require(valid_disk(device) and parse_probe(remote(ssh, 20, PROBE, device, capture=True), device) == disk,
                "Rescue disk identity changed")
    with install_stage("store-service"):
        if ram:
            remote(ssh, remaining(directory, binding, 20), RAM_SERVE, str(target_seconds + 10))
        else:
            require(archive is not None, "Rescue Nix archive is required")
            descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= 100_000_000
                        and hashlib.file_digest(source, "sha256").hexdigest() == NIX_SHA,
                        "Rescue Nix archive rejected")
                source.seek(0)
                remote(ssh, remaining(directory, binding, 120), BOOTSTRAP, str(target_seconds + 10), stdin=source)
    with install_stage("store-copy"):
        copy_store(binding, options, remaining(directory, binding, 240), [plan["disko"], plan["installer"]])
    with install_stage("recheck"):
        # Provider controller may refresh observedAt. The immutable binding and
        # Rescue pin must still match, immediately before any disk write.
        fresh, fresh_key, _, _ = authority(directory, phase, role, ram=ram)
        require(fresh == binding and fresh_key == key, "install target changed before formatting")
        require(parse_probe(remote(ssh, 20, PROBE, device, capture=True), device) == disk,
                "Rescue disk identity changed before formatting")
        write_private(directory / f"{prefix}-install-dispatched.json", {
            "binding": binding, "diskSha256": disk_digest, "planSha256": approved,
        })
    disk_args = [disk["bootId"], device, disk["canonical"], disk["majorMinor"], str(disk["size"])]
    with install_stage("format"):
        run_step(ssh, remaining(directory, binding, 90), "format", FORMAT, *disk_args, plan["disko"])
    with install_stage("target-copy"):
        copy_store(binding, options, remaining(directory, binding, target_seconds), [plan["system"]], target=True)
    with install_stage("finish"), tempfile.TemporaryFile() as key_stream:
        key_stream.write(key_bytes)
        key_stream.seek(0)
        run_step(ssh, remaining(directory, binding, 180), "finish", FINISH, *disk_args, plan["installer"],
                 plan["system"], public_data, stdin=key_stream)
    with install_stage("installed-receipt"):
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
            install(directory, args.phase, args.role, args.install_approved_sha, args.bootstrap_archive)
            message = "Reviewed NixOS installed to the verified disk. Reboot is separate."
    except InstallFailed as failure:
        print(failure.report(), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print("Rescue operation rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
