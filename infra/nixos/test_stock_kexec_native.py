"""Prove the strict stock-to-RAM SSH handoff with one isolated Debian guest."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from unittest.mock import patch

import rehearsal_services as services
import rescue_install as install
import stock_kexec
from test_stock_kexec_packets import EXPECTED, SNAPLEN, packet_facts, packet_pipe


BASE_SHA512 = (
    "d25999de0770d4cde1be72c299ba3ad94eac7ea8fd4d18f8cc86c69df69337e2e8"
    "a91d64465a9bfd8f2a78c566fcbcb0842eacc91c09efdef72d719d9f13b535"
)
ALIAS = "terrace-stock-kexec-fixture"
FIXTURE_RUN = "f1" * 16
PROOF_CONTEXT = (FIXTURE_RUN, "original", "relay")
PREFIX = "original-relay"
# Shaped like Hetzner's CPX42 app disk: one 320 GB-class SCSI disk, the stock image's GPT
# layout grown by cloud-init, and a CD-ROM beside it.
FIXTURE_DISK_GB = 320
FIXTURE_DISK = "/dev/sda"
ESP_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
PARTITION_TYPES = {ESP_TYPE: "esp", "21686148-6449-6e6f-744e-656564454649": "bios-boot",
                   "4f68bce3-e8cd-4dd1-9616-60ef3a5b7e9b": "linux-root-x86-64",
                   "0fc63daf-8483-4772-8e79-3d69d8477de4": "linux-data"}
FIXTURE_MAC = "52:54:00:12:34:56"
FIXTURE_IP = socket.inet_ntoa(EXPECTED)
SERIAL_LIMIT = 256 * 1024
SERIAL_MARKERS = {
    "seaBios": (b"SeaBIOS",),
    "firmwareDiskBoot": (b"Booting from Hard Disk",),
    "uefiFirmware": (b"BdsDxe", b"EDK II"),
    "grub": (b"GRUB",),
    "loaderError": (b"no such device", b"unknown filesystem", b"no bootable device",
                    b"you need to load the kernel first", b"disk read error", b"grub rescue>"),
    "linuxKernel": (b"Linux version ", b"Booting Linux"),
    "kernelPanic": (b"Kernel panic",),
    "initStarted": (b"Run /init as init process", b"systemd[1]: "),
    "cloudInit": (b"Cloud-init v", b"cloud-init["),
    "noCloud": (b"DataSourceNoCloud",),
    "sshService": (b"OpenBSD Secure Shell server", b"sshd["),
}
RAM_SERIAL_MARKERS = {
    "kexec": (b"kexec",),
    "nixos": (b"nixos",),
    "initrdStarted": (b"running in initrd", b"initrd-root-fs.target"),
    "rootSwitch": (b"switching root", b"initrd-switch-root.service"),
    "restoreNetwork": (b"restore-network",),
    "networkd": (b"systemd-networkd",),
    "sshService": SERIAL_MARKERS["sshService"],
    "kernelPanic": SERIAL_MARKERS["kernelPanic"],
}
BOOT_UNITS = {
    "restoreState": (b"restore-state-from-initrd.service",),
    "restoreNetwork": (b"restore-network.service",),
    "networkd": (b"systemd-networkd.service", b"network configuration"),
    "sshdKeygen": (b"sshd-keygen.service", b"ssh host keys generation"),
    "sshd": (b"sshd.service", b"ssh daemon"),
    "multiUser": (b"multi-user.target", b"multi-user system"),
}
RAM_MOUNTS = ("/", "/nix/.ro-store", "/nix/.rw-store", "/nix/store")
MOUNT_TYPES = {"tmpfs", "ramfs", "rootfs", "squashfs", "overlay", "ext4"}
CLOUD_INIT_STATES = {
    "not started", "running", "done", "error", "disabled", "error - done",
    "error - running", "degraded done", "degraded running",
}
# The relay role is installed exactly as the private planner prepares it: this module
# shape, the relay's synthetic private address, and the planner's three outputs.
RELAY_PRIVATE_IPV4 = "10.71.0.8"
HARDWARE_MODULE = """{{ modulesPath, ... }}: {{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  networking.useDHCP = true;
  terrace.cloudDisk.device = {device};
  terrace.hetzner.authorizedKeys = [ {key} ];
}}
"""
LOADER_IMAGE = "/sys/firmware/efi/efivars/LoaderImageIdentifier-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
# What the pre-fix text guards would have compared on this RAM installer, as booleans only.
LEGACY_GUARD_FACTS = """
set -euo pipefail
printf '[%s]\n' "$(lsblk -dn -o MAJ:MIN -- "$1")"
printf '[%s]\n' "$(lsblk -drn -o MAJ:MIN -- "$1")"
"""
CAPACITY_PROBE = """
set -eu
printf '%s\n' "$(findmnt -rn -o FSTYPE --mountpoint /run || true)"
printf '%s\n' "$(findmnt -rn -o FSTYPE --mountpoint /dev/shm || true)"
df -PB1 /run | awk 'NR == 2 {print $4}'
df -PB1 /dev/shm | awk 'NR == 2 {print $4}'
awk '/^MemAvailable:/ {print $2}' /proc/meminfo
"""


def base_digest(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as image:
        info = os.fstat(image.fileno())
        install.require(stat.S_ISREG(info.st_mode) and 1 <= info.st_size <= 1_073_741_824,
                        "Debian base is not the expected bounded file")
        return hashlib.file_digest(image, "sha512").hexdigest()


def key(path):
    install.checked_process(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], 5)
    return " ".join(path.with_suffix(".pub").read_text().split()[:2])


def private_text(path, text):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(text)


def render_seed(args, directory, seed, nonce, operator_public):
    renderer = Path(__file__).resolve().parents[1] / "cloud/rehearsal-create/bootstrap.ts"
    script = """
import { pathToFileURL } from "node:url";
const [renderer, noncePath, run, phase, role, operatorPublicKey] = process.argv.slice(1);
const { renderBootstrapCloudConfig } = await import(pathToFileURL(renderer).href);
const nonce = (await Bun.file(noncePath).text()).trim();
process.stdout.write(renderBootstrapCloudConfig({ nonce, run, phase, role }, operatorPublicKey));
"""
    user_data = install.checked_process(
        ["bun", "-e", script, str(renderer), str(directory / f"{PREFIX}-bootstrap-nonce"),
         *PROOF_CONTEXT, operator_public], 10, capture=True
    ).decode("utf-8")
    install.require("PRIVATE KEY" not in user_data and nonce in user_data, "unexpected bootstrap user data")
    install.require(user_data.startswith("#cloud-config\n"), "creator did not return cloud config")
    data = directory / "user-data"
    metadata = directory / "meta-data"
    network = directory / "network-config"
    private_text(data, user_data)
    # Like Hetzner metadata, offer the project SSH key; user data refuses to import it.
    private_text(metadata, f"instance-id: {ALIAS}\nlocal-hostname: {ALIAS}\n"
                           f"public-keys: {json.dumps([operator_public])}\n")
    private_text(network, json.dumps({
        "version": 2,
        "ethernets": {"fixture": {
            "match": {"macaddress": FIXTURE_MAC}, "dhcp4": False, "dhcp6": False,
            "addresses": [FIXTURE_IP + "/24"],
            "routes": [{"to": "0.0.0.0/0", "via": "10.0.2.2"}],
        }},
    }) + "\n")
    with seed.open("xb") as stream:
        stream.truncate(2 * 1024 * 1024)
    # The seed is a CD-ROM (2048-byte blocks), so the filesystem's sectors must be that large.
    install.checked_process([args.mkfs_vfat, "-S", "2048", "-n", "CIDATA", str(seed)], 15)
    install.checked_process(
        [args.mcopy, "-i", str(seed), str(data), str(metadata), str(network), "::/"], 15
    )


@contextmanager
def guest(args, overlay, seed, serial, facts, qmp, efi_vars):
    # Hetzner's server types boot UEFI, so the fixture does too: read-only OVMF code
    # and this run's private copy of the variable store, like a server's NVRAM.
    command = [
        args.qemu, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "4096", "-smp", "2",
        "-nodefaults", "-boot", "order=c", "-vga", "std", "-display", "none",
        "-monitor", "none", "-serial", "stdio",
        "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={args.ovmf_code}",
        "-drive", f"if=pflash,format=raw,unit=1,file={efi_vars}",
        "-qmp", f"unix:{qmp},server=on,wait=off",
        # One SCSI disk and a SCSI CD-ROM, like a Hetzner server's sda and sr0.
        "-device", "virtio-scsi-pci,id=scsi0",
        "-drive", f"file={overlay},format=qcow2,if=none,id=root",
        "-device", "scsi-hd,drive=root,bus=scsi0.0,bootindex=0",
        "-drive", f"file={seed},format=raw,if=none,id=seed,readonly=on",
        "-device", "scsi-cd,drive=seed,bus=scsi0.0",
        "-netdev", f"user,id=proofnet,restrict=on,hostfwd=tcp:127.0.0.1:{args.port}-{FIXTURE_IP}:22",
        "-device", f"virtio-net-pci,netdev=proofnet,mac={FIXTURE_MAC}",
    ]
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True) as process:
        def drain():
            for data in iter(lambda: process.stdout.read1(8192), b""):
                serial.extend(data[:max(0, SERIAL_LIMIT - len(serial))])

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            facts["qemuPid"] = process.pid
            facts["qemuStart"] = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            reader.join(timeout=5)
            facts["qemuOff"] = process.poll() is not None and not Path(f"/proc/{process.pid}").exists()
            install.require(facts["qemuOff"] and not reader.is_alive(), "offline guest did not stop")


def await_proof(process, seconds, nonce, target):
    """Wait for the guest's keyed banner; a wrong proof fails at once."""
    deadline = time.monotonic() + seconds
    while process.poll() is None and time.monotonic() < deadline:
        key = install.verified_host_key(nonce, PROOF_CONTEXT, "root@127.0.0.1", target, 20)
        if key is not None:
            return key
        time.sleep(2)
    raise install.Rejected("offline guest did not publish its host key proof")


def await_fact(process, seconds, action):
    deadline = time.monotonic() + seconds
    while process.poll() is None and time.monotonic() < deadline:
        try:
            return action()
        except install.Rejected:
            time.sleep(2)
    raise install.Rejected("offline guest did not reach the required stage")


def wrong_host_rejected(ssh):
    probe = subprocess.run([*ssh, "true"], stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    install.require(probe.returncode == 255 and not probe.stdout
                    and b"Host key verification failed." in probe.stderr,
                    "wrong host key was not rejected by OpenSSH")


def fixture_ipv4_facts(data, *, static):
    interfaces = json.loads(data)
    matches = [row for row in interfaces if row.get("address") == FIXTURE_MAC]
    if len(matches) != 1:
        return False
    addresses = [row for row in matches[0].get("addr_info", []) if row.get("family") == "inet"]
    return len(addresses) == 1 and (
        addresses[0]["family"], addresses[0]["local"], addresses[0]["prefixlen"]
    ) == ("inet", FIXTURE_IP, 24) and (not static or addresses[0].get("dynamic", False) is False)


def require_fixture_ipv4(ssh, *, static):
    data = install.remote(ssh, 8, "ip --json addr", capture=True)
    install.require(fixture_ipv4_facts(data, static=static), "fixture IPv4 was not observed")


def diagnostic_command(argv, seconds, *, stdin=None, truncate=False):
    deadline = time.monotonic() + seconds
    output = [bytearray(), bytearray()]
    with subprocess.Popen(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as process, selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ, 0)
        ready.register(process.stderr, selectors.EVENT_READ, 1)
        try:
            while ready.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    return 124, b"", b""
                for stream, _ in ready.select(left):
                    data = os.read(stream.fd, 8192)
                    if not data:
                        ready.unregister(stream.fd)
                        continue
                    buffer = output[stream.data]
                    space = 8192 - len(buffer)
                    if len(data) > space and not truncate:
                        return 125, b"", b""
                    buffer.extend(data[:space])
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            return code, bytes(output[0]), bytes(output[1])
        except subprocess.TimeoutExpired:
            return 124, b"", b""
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


def cloud_status_facts(code, data):
    facts = {"statusReadRc": code, "status": None, "extendedStatus": None,
             "fatalErrorCount": None, "recoverableErrorCount": None}
    try:
        status = json.loads(data)
    except (ValueError, RecursionError):
        return facts
    if not isinstance(status, dict):
        return facts
    for source, target in (("status", "status"), ("extended_status", "extendedStatus")):
        value = status.get(source)
        if isinstance(value, str) and value in CLOUD_INIT_STATES:
            facts[target] = value
    errors = status.get("errors")
    if isinstance(errors, list):
        facts["fatalErrorCount"] = len(errors)
    recoverable = status.get("recoverable_errors")
    if isinstance(recoverable, dict) and all(isinstance(items, list) for items in recoverable.values()):
        facts["recoverableErrorCount"] = sum(map(len, recoverable.values()))
    return facts


def ssh_diagnostics(port, ssh, public):
    facts = {"tcpConnected": False, "sshBanner": False, "ed25519Match": None,
             "pinnedSsh": "unobserved"}
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
            facts["tcpConnected"] = True
            with conn.makefile("rb") as stream:
                facts["sshBanner"] = stream.readline(256).startswith(b"SSH-2.0-")
    except OSError:
        pass
    if not facts["sshBanner"]:
        return facts
    code, out, _ = diagnostic_command(
        ["ssh-keyscan", "-q", "-T", "3", "-p", str(port), "-t", "ed25519", "127.0.0.1"], 5
    )
    facts["ed25519Match"] = code == 0 and len(out.splitlines()) == 1 and (
        out.split()[1:] == [b"ssh-ed25519", public.encode("ascii")]
    )
    code, _, err = diagnostic_command([*ssh, "true"], 15)
    if code == 0:
        facts["pinnedSsh"] = "authenticated"
    elif b"Host key verification failed." in err:
        facts["pinnedSsh"] = "host-key-rejected"
    elif b"Permission denied (publickey)" in err:
        facts["pinnedSsh"] = "public-key-rejected"
    else:
        facts["pinnedSsh"] = "timeout" if code == 124 else "transport-error"
    return facts


def stock_diagnostics(port, ssh, public, operator):
    facts = ssh_diagnostics(port, ssh, public)
    facts.update(stockCheckLine=None, stockCheckRc=None)
    if facts["pinnedSsh"] == "authenticated":
        # Repeat the same read-only checks; the probe itself reports a failed line.
        probe = stock_kexec.STOCK_PROBE
        command = shlex.join(["timeout", "20", "bash", "-c", probe, "terrace", public, operator])
        _, out, _ = diagnostic_command([*ssh, command], 25)
        match = stock_kexec.STOCK_FAILURE.fullmatch(out)
        if match:
            line, rc = map(int, match.groups())
            facts.update(stockCheckLine=line, stockCheckRc=rc)
            lines = probe.splitlines()
            start = lines.index("cloud_init=0")
            if start < line <= lines.index("fi", start) + 1:
                code, status, _ = diagnostic_command([*ssh, "timeout 10 cloud-init status --format json"], 15)
                facts["cloudInitStatus"] = cloud_status_facts(code, status)
    return facts


def ram_diagnostics(port, ssh, public, old_boot):
    facts = ssh_diagnostics(port, ssh, public)
    facts.update(bootIdChanged=None, ramCheckLine=None, ramCheckRc=None)
    if facts["pinnedSsh"] == "authenticated":
        code, out, _ = diagnostic_command([*ssh, "timeout 5 cat /proc/sys/kernel/random/boot_id"], 10)
        boot = out.decode("ascii", errors="replace").strip()
        if code == 0 and install.BOOT_ID.fullmatch(boot):
            facts["bootIdChanged"] = boot != old_boot
        probe = """set -Eeuo pipefail
trap 'printf "ram-line:%d:%d\\n" "$LINENO" "$?"' ERR
""" + install.RAM_PROBE
        command = shlex.join(["timeout", "15", "bash", "-c", probe])
        code, out, _ = diagnostic_command([*ssh, command], 20)
        facts["ramCheckRc"] = code
        match = re.fullmatch(rb"ram-line:([1-9][0-9]{0,2}):[1-9][0-9]{0,2}\n", out)
        if match:
            facts["ramCheckLine"] = int(match[1])
            facts["ramMountTypes"] = ram_mount_types(ssh)
    return facts


def ram_mount_types(ssh):
    """Return allowlisted filesystem types stacked at each RAM installer mountpoint."""
    probe = 'for m in "$@"; do findmnt -rn -o FSTYPE --mountpoint "$m" | head -n 4 | tr "\\n" " "; echo; done'
    command = shlex.join(["timeout", "5", "bash", "-c", probe, "terrace", *RAM_MOUNTS])
    code, out, _ = diagnostic_command([*ssh, command], 10)
    lines = out.decode("ascii", errors="replace").splitlines()
    if code != 0 or len(lines) != len(RAM_MOUNTS):
        return None
    return {mount: [kind if kind in MOUNT_TYPES else "other" for kind in line.split()]
            for mount, line in zip(RAM_MOUNTS, lines)}


def boot_unit_facts(data):
    data = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", data).lower()
    units = {name: {"begin": False, "success": False, "failure": False} for name in BOOT_UNITS}
    lines = data.splitlines()
    for line in lines:
        for name, markers in BOOT_UNITS.items():
            if not any(marker in line for marker in markers):
                continue
            states = units[name]
            states["begin"] |= b"starting " in line
            states["success"] |= any(marker in line for marker in (b"started ", b"finished ", b"reached target "))
            states["failure"] |= b"failed" in line or b"timed out" in line
    return {
        "units": units,
        "sshHostKeyError": any(marker in data for marker in (b"no hostkeys available", b"unable to load host key")),
        "sshBindError": any(
            b"bind to port 22" in line and any(marker in line for marker in (
                b"failed", b"address already in use", b"cannot assign requested address"
            )) for line in lines
        ),
        "dhcpError": any(b"dhcp" in line and (b"failed" in line or b"timed out" in line) for line in lines),
        "waitOnlineTimedOut": any(b"wait-online" in line and b"timed out" in line for line in lines),
    }


@contextmanager
def qmp_commands(qmp):
    deadline = time.monotonic() + 6
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(2)
        conn.connect(str(qmp))
        with conn.makefile("rb", buffering=0) as stream:
            def read():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                conn.settimeout(left)
                return json.loads(stream.readline(16384))

            def ask(command, **args):
                conn.sendall(json.dumps({"execute": command, "arguments": args}).encode() + b"\n")
                while True:
                    event = read()
                    if "return" in event:
                        return event["return"]
                    if "error" in event:
                        return None

            install.require("QMP" in read() and ask("qmp_capabilities") == {}, "private QMP handshake failed")
            yield ask


def qmp_failure_facts(qmp):
    memory = qmp.parent / "vga.bin"
    try:
        with qmp_commands(qmp) as ask:
            status = ask("query-status")
            saved = ask("pmemsave", val=0xb8000, size=4000, filename=str(memory))
        data = b""
        if saved == {}:
            with memory.open("rb") as stream:
                raw = stream.read(4001)
            if len(raw) == 4000:
                cells = raw[::2].lower()
                data = b"\n".join(cells[i:i + 80].strip(b"\x00 ") for i in range(0, len(cells), 80))
        return {"running": isinstance(status, dict) and status.get("status") == "running",
                "vgaTextRead": any(33 <= char < 127 for char in data), "nixos": b"nixos" in data,
                "loginPrompt": b"login:" in data,
                "emergencyMode": b"emergency mode" in data,
                "bootUnits": boot_unit_facts(data)}
    except (OSError, ValueError, TypeError, RecursionError):
        return None
    finally:
        memory.unlink(missing_ok=True)


def start_packet_capture(qmp):
    try:
        with qmp_commands(qmp) as ask:
            return ask("object-add", **{
                "qom-type": "filter-dump", "id": "proof-headers", "netdev": "proofnet",
                "file": str(qmp.parent / "headers.pipe"), "maxlen": SNAPLEN, "queue": "all",
            }) == {}
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def capacity_facts(data):
    fields = data.splitlines()
    if len(fields) != 5 or any(not re.fullmatch(rb"[0-9]{1,16}", value) for value in fields[2:]):
        return None
    run_free, shm_free, memory = map(int, fields[2:])
    return {"runTmpfs": fields[0] == b"tmpfs", "shmTmpfs": fields[1] == b"tmpfs",
            "runFreeBytes": run_free, "shmFreeBytes": shm_free, "memAvailableKiB": memory}


def observe_upload(facts, remote, ssh, seconds, script, *args, stdin=None, capture=False):
    if script != stock_kexec.UPLOAD:
        return remote(ssh, seconds, script, *args, stdin=stdin, capture=capture)
    # Observe this exact mutation only once. A failed upload is never retried.
    script = script.replace(
        "set -euo pipefail", """set -Eeuo pipefail
trap 'printf "upload-line:%d:%d\\n" "$LINENO" "$?"' ERR""", 1
    )
    command = shlex.join(["timeout", "--kill-after=10", str(seconds), "bash", "-c",
                          script, "terrace", *args])
    code, output, _ = diagnostic_command([*ssh, command], seconds + 20, stdin=stdin)
    if code != 0:
        result = {"commandRc": code, "uploadLine": None, "uploadRc": None}
        matches = re.findall(rb"^upload-line:([1-9][0-9]{0,2}):([1-9][0-9]{0,2})$", output, re.M)
        if matches:
            result["uploadLine"], result["uploadRc"] = map(int, matches[-1])
        facts["uploadDiagnostics"] = result
        try:
            result["capacity"] = capacity_facts(remote(ssh, 8, CAPACITY_PROBE, capture=True))
        except install.Rejected:
            result["capacity"] = None
        raise install.Rejected("verified archive upload rejected")
    facts["archiveUploadPassed"] = True


def observe_dispatch(facts, serial, qmp, remote, ssh, seconds, script, *args, stdin=None, capture=False):
    if script != stock_kexec.KEXEC:
        return observe_upload(facts, remote, ssh, seconds, script, *args, stdin=stdin, capture=capture)
    facts["packetCaptureStarted"] = start_packet_capture(qmp)
    facts["postDispatchSerialStart"] = len(serial)
    command = shlex.join(["timeout", "--kill-after=10", str(seconds), "bash", "-c",
                          script, "terrace", *args])
    code, out, err = diagnostic_command([*ssh, command], seconds + 20, truncate=True)
    facts["kexecInvocation"] = {
        "commandRc": code,
        "scheduled": b"machine will boot into nixos" in out,
        "loadFailed": b"kexec failed, dumping dmesg" in out + err,
        "operationNotPermitted": b"operation not permitted" in (out + err).lower(),
    }
    install.require(code == 0, "kexec invocation did not return success")


def target_label(ssh, ipv4, capacity):
    try:
        stock_kexec.ram_target_check(ssh, ipv4, capacity)
    except stock_kexec.TargetCheckFailed as failure:
        return failure.label
    return None


def await_target_label(ssh, label, seconds=30):
    deadline = time.monotonic() + seconds
    while (observed := target_label(ssh, FIXTURE_IP, FIXTURE_DISK_GB)) != label:
        install.require(time.monotonic() < deadline, "install target did not reach the expected state")
        time.sleep(1)
    return observed


def stock_layout(ssh):
    """The stock image's partitions as the RAM installer sees them: types, filesystems, sizes."""
    raw = install.remote(ssh, 20, 'lsblk --tree --bytes --json --paths --output PATH,TYPE,SIZE,PARTTYPE,FSTYPE -- "$1"',
                         FIXTURE_DISK, capture=True)
    disk, = json.loads(raw)["blockdevices"]
    parts = [{"path": row["path"], "type": PARTITION_TYPES.get((row.get("parttype") or "").lower(), "other"),
              "fstype": row.get("fstype"), "sizeMiB": row["size"] // (1024 * 1024)}
             for row in disk.get("children", [])]
    return disk["size"], parts


def block_devices(ssh):
    """Diagnostic only: each whole device's path, type and size."""
    try:
        raw = install.remote(ssh, 20, "lsblk --bytes --json --paths --nodeps --output PATH,TYPE,SIZE,RO,RM,MOUNTPOINTS",
                             capture=True)
        return [[row["path"], row["type"], row["size"], row["ro"], row["rm"], row["mountpoints"]]
                for row in json.loads(raw)["blockdevices"]]
    except (install.Rejected, ValueError, KeyError, TypeError):
        return None


def exercise_target(directory, ssh, public, options, qmp, facts):
    """The install plan's RAM target checks and whole-disk probe against the stock disk."""
    facts["ramTargetStep"] = "target"
    try:
        target = stock_kexec.ram_target_check(ssh, FIXTURE_IP, FIXTURE_DISK_GB)
    except stock_kexec.TargetCheckFailed as failure:
        facts["ramTargetFailure"] = {"label": failure.label, "line": failure.line, "status": failure.status}
        facts["ramBlockDevices"] = block_devices(ssh)
        raise
    facts["ramTarget"] = target
    install.require(target == {"device": FIXTURE_DISK, "size": FIXTURE_DISK_GB * stock_kexec.GIB},
                    "fixture install target differs")
    facts["ramTargetStep"] = "layout"
    try:
        size, parts = stock_layout(ssh)
    except (install.Rejected, ValueError, KeyError, TypeError) as error:
        # Diagnostic only: the error class and, for a remote rejection, the fixed message.
        facts["stockLayoutError"] = [type(error).__name__, str(error) if isinstance(error, install.Rejected) else None]
        facts["ramBlockDevices"] = block_devices(ssh)
        raise
    facts["stockDiskBytes"] = size
    facts["stockLayout"] = [{key: value for key, value in part.items() if key != "path"} for part in parts]
    kinds = [part["type"] for part in parts]
    esp = next((part for part in parts if part["type"] == "esp"), None)
    install.require(size == target["size"] and esp is not None and esp["fstype"] == "vfat"
                    and any(part["fstype"] == "ext4" and part["sizeMiB"] > 300 * 1024 for part in parts),
                    "stock layout lacks its ESP or grown ext4 root")
    facts["ramTargetStep"] = "rejects"
    rejects = {"routeSource": target_label(ssh, "10.0.2.99", FIXTURE_DISK_GB),
               "diskSize": target_label(ssh, FIXTURE_IP, 80)}
    # A second disk, hot-plugged and removed again, must stop the plan.
    with qmp_commands(qmp) as ask:
        install.require(ask("blockdev-add", driver="null-co", size=1024 ** 3, **{"node-name": "extra"}) == {}
                        and ask("device_add", driver="scsi-hd", drive="extra", id="extra0", bus="scsi0.0") == {},
                        "second disk hot-plug failed")
    facts["ramTargetStep"] = "second-disk"
    rejects["secondDisk"] = await_target_label(ssh, "single-disk")
    with qmp_commands(qmp) as ask:
        install.require(ask("device_del", id="extra0") == {}, "second disk removal failed")
    await_target_label(ssh, None)
    with qmp_commands(qmp) as ask:
        install.require(ask("blockdev-del", **{"node-name": "extra"}) == {}, "second disk node survived")
    facts["ramTargetRejects"] = rejects
    facts["ramTargetStep"] = "whole-disk"
    bound = ({"ipv4": FIXTURE_IP}, public, options, ssh)
    with patch.object(install, "authority", return_value=bound), \
            patch.object(install, "reviewed_probe_mode", return_value=False), \
            patch.object(install, "reviewed_bootstrap", return_value=(FIXTURE_RUN, "0" * 64)):
        # A mounted stock partition must stop the whole-disk probe.
        install.remote(ssh, 20, 'set -eu; mkdir -p /run/terrace-esp-check; mount -o ro "$1" /run/terrace-esp-check',
                       esp["path"])
        try:
            stock_kexec.inspect_ram_target(directory, "original", "relay", FIXTURE_DISK_GB)
        except stock_kexec.TargetCheckFailed as failure:
            rejects["mountedPartition"] = failure.label
        finally:
            install.remote(ssh, 20, "set -eu; umount /run/terrace-esp-check; rmdir /run/terrace-esp-check")
        _, _, disk, _ = stock_kexec.inspect_ram_target(directory, "original", "relay", FIXTURE_DISK_GB)
    facts["ramTargetRejects"] = rejects
    install.require(rejects == {"routeSource": "route-source", "diskSize": "disk-size",
                                "secondDisk": "single-disk", "mountedPartition": "probe-partition-state"},
                    "an install target fault was not reported by its label")
    install.require(disk["canonical"] == FIXTURE_DISK and disk["size"] == target["size"],
                    "whole-disk receipt differs from the target")
    facts["wholeDiskProbe"] = {"partitions": len(kinds), "kinds": sorted(set(kinds)), "receipt": True}


def build_install_plan(args, directory, operator_public):
    """Build the relay's toplevel, Disko script and installer like the private planner."""
    module = directory / f"{PREFIX}-hardware.nix"
    private_text(module, HARDWARE_MODULE.format(device=json.dumps(FIXTURE_DISK), key=json.dumps(operator_public)))
    argv = ["nix", "build", "--offline", "--impure", "--option", "substitute", "false", "--option", "builders", "",
            "--option", "post-build-hook", "", "--max-jobs", "1", "--cores", "2",
            "--file", "infra/nixos/rehearsal-config.nix", "--arg", "localModule", str(module),
            "--argstr", "role", "relay", "--argstr", "privateIPv4", RELAY_PRIVATE_IPV4,
            "--out-link", str(directory / "relay-closure"), "--print-out-paths",
            "config.system.build.toplevel", "config.system.build.diskoScript", "config.system.build.nixos-install"]
    result = subprocess.run(argv, cwd=args.install_source, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=900, check=False)
    install.require(result.returncode == 0, "relay install outputs did not build")
    paths = result.stdout.decode("ascii").split()
    install.require(len(paths) == 3 and all(install.STORE.fullmatch(path) for path in paths),
                    "unexpected relay install outputs")
    plan = {"system": next(path for path in paths if "-nixos-system-relay-" in path),
            "disko": next(path for path in paths if path.endswith("-disko")),
            "installer": next(path for path in paths if path.endswith("-nixos-install"))}
    install.require(len(set(plan.values())) == 3, "relay install outputs are not distinct")
    return plan


def legacy_guard_facts(ssh, target):
    """Booleans only: whether the table and raw MAJ:MIN listings equal the probe's JSON value."""
    table, raw = install.remote(ssh, 20, LEGACY_GUARD_FACTS, target["canonical"],
                                capture=True).decode("ascii").splitlines()
    expected = f"[{target['majorMinor']}]"
    return {"tableMajMinMatches": table == expected, "tableMajMinPadded": table.replace(" ", "") == expected,
            "rawMajMinMatches": raw == expected}


def step_label(ssh, name, script, *args):
    try:
        install.run_step(ssh, 60, name, script, *args)
    except install.InstallFailed as failure:
        return failure.label, failure.line
    return None


def exercise_install(directory, ssh, public, options, plan, installed_public, facts):
    """The provider install path on the RAM installer: store copy, FORMAT, target copy, FINISH."""
    reviewed, disk_digest = install.read_json(directory / f"{PREFIX}-disk.json")
    target = reviewed["disk"]
    facts["legacyGuards"] = legacy_guard_facts(ssh, target)
    disk_args = [target["bootId"], target["device"], target["canonical"], target["majorMinor"], str(target["size"])]
    # Both disk steps stop at their guards, before any write, and name the line.
    lines = {name: script.splitlines() for name, script in (("format", install.FORMAT), ("finish", install.FINISH))}
    rejects = {
        "formatWrongMajorMinor": step_label(ssh, "format", install.FORMAT, *disk_args[:3], "0:0", disk_args[4],
                                            plan["disko"]),
        "finishWrongBoot": step_label(ssh, "finish", install.FINISH, "0" * 8 + target["bootId"][8:],
                                      *disk_args[1:], plan["installer"], plan["system"], installed_public),
    }
    facts["installGuardRejects"] = rejects
    install.require(rejects == {
        "formatWrongMajorMinor": ("format", lines["format"].index(
            'test "$(lsblk -drn -o MAJ:MIN -- "$3")" = "$4"') + 1),
        "finishWrongBoot": ("finish", lines["finish"].index(
            'test "$(cat /proc/sys/kernel/random/boot_id)" = "$1"') + 1),
    }, "a disk step guard did not report its line")
    private_text(directory / f"{PREFIX}-installed-known-hosts", f"{FIXTURE_IP} ssh-ed25519 {installed_public}\n")
    install.write_private(directory / f"{PREFIX}-install.json", {"diskSha256": disk_digest, **plan})
    _, approved = install.read_json(directory / f"{PREFIX}-install.json")
    bound = ({"ipv4": FIXTURE_IP}, public, options, ssh)
    copy = install.copy_store

    def loopback_copy(binding, copy_options, seconds, paths, *, target=False):
        # The guest is reached through its loopback forward; the pinned SSH options are unchanged.
        return copy({**binding, "ipv4": "127.0.0.1"}, copy_options, seconds, paths, target=target)

    start = time.monotonic()
    with patch.object(install, "authority", return_value=bound), \
            patch.object(install, "reviewed_probe_mode", return_value=False), \
            patch.object(install, "reviewed_bootstrap", return_value=(FIXTURE_RUN, "0" * 64)), \
            patch.object(install, "copy_store", side_effect=loopback_copy):
        try:
            install.install(directory, "original", "relay", approved, None)
        except install.InstallFailed as failure:
            facts["installFailure"] = {"label": failure.label, "line": failure.line, "status": failure.status}
            raise
    facts["installSeconds"] = round(time.monotonic() - start)
    installed, _ = install.read_json(directory / f"{PREFIX}-installed.json")
    install.require(installed["system"] == plan["system"] and installed["installedHostKey"] == installed_public
                    and installed["mountedExt4Verified"] is True, "installed receipt differs from the plan")
    facts["installed"] = {"receipt": True, "planSha256": approved == installed["planSha256"]}
    return installed, target["bootId"]


def exercise_installed_boot(args, directory, process, qmp, ssh, installed, ram_boot, facts):
    """Reset the guest like the provider's reboot; the disk must boot the installed relay."""
    private_text(directory / f"{PREFIX}-booted-known-hosts", f"{ALIAS} ssh-ed25519 {installed['installedHostKey']}\n")
    booted = ["ssh", *install.ssh_options(directory, PREFIX, "booted"), "-o", "HostKeyAlias=" + ALIAS,
              "-p", str(args.port), "operator@127.0.0.1"]
    with qmp_commands(qmp) as ask:
        install.require(ask("system_reset") == {}, "guest reset failed")
    start = time.monotonic()
    # The same first check the service phase runs on each installed host.
    await_fact(process, 300, lambda: install.remote(booted, 20, services.INSTALLED_IDENTITY,
                                                    installed["system"], "relay"))
    facts["installedBootSeconds"] = round(time.monotonic() - start)
    facts["installedIdentity"] = True
    boot = install.remote(booted, 10, "cat /proc/sys/kernel/random/boot_id", capture=True).decode("ascii").strip()
    install.require(install.BOOT_ID.fullmatch(boot) and boot != ram_boot, "installed system did not boot anew")
    install.remote(booted, 10, "test -d /sys/firmware/efi")
    raw = install.remote(booted, 10, 'if test -e "$1"; then base64 -w0 "$1"; fi', LOADER_IMAGE, capture=True)
    loader = base64.b64decode(raw)[4:].decode("utf-16-le").rstrip("\0") if raw else None
    # No firmware variable names this disk: the firmware must have used the removable path.
    facts["installedLoaderImage"] = loader
    install.require(loader is not None and loader.lower() == "\\efi\\boot\\bootx64.efi",
                    "installed system did not boot from the fallback loader")
    state = install.remote(booted, 15, "systemctl is-system-running || true", capture=True).decode("ascii").strip()
    facts["installedSystemState"] = state if state in {"running", "degraded", "starting"} else "other"
    wrong_host_rejected(ssh)
    facts["installedRejectsRamPin"] = True
    return booted


RELAY_UNITS = ("headscale", "nginx", "cloud-rehearsal-tls", "cloud-rehearsal-relay-address")
RELAY_TOOLS = ("headscale", "jq", "curl", "sqlite3")


def relay_diagnostics(booted):
    """Diagnostic only: the failing bootstrap line, fixed unit states and tool presence."""
    facts = {}
    probe = "set -Eeuo pipefail\ntrap 'printf \"relay-line:%d:%d\\n\" \"$LINENO\" \"$?\"' ERR\n" + services.BOOTSTRAP_RELAY
    command = shlex.join(["timeout", "120", "sudo", "-n", "bash", "-c", probe])
    _, out, _ = diagnostic_command([*booted, command], 130, truncate=True)
    match = re.search(rb"^relay-line:([1-9][0-9]{0,2}):([1-9][0-9]{0,2})$", out, re.M)
    facts["line"] = list(map(int, match.groups())) if match else None
    states = {"active", "inactive", "failed", "activating", "deactivating", "success", "exit-code", "timeout",
              "signal", "core-dump", "start-limit-hit", "resources", "dependency"}
    _, out, _ = diagnostic_command([*booted, shlex.join(["systemctl", "show", "-p", "ActiveState", "-p", "Result",
                                                         *(name + ".service" for name in RELAY_UNITS)])], 15)
    values = [line.split("=", 1)[1] for line in out.decode("ascii", "replace").splitlines() if "=" in line]
    facts["units"] = [value if value in states else "other" for value in values]
    _, out, _ = diagnostic_command([*booted, "for t in " + " ".join(RELAY_TOOLS) +
                                    "; do command -v $t >/dev/null && echo y || echo n; done"], 15)
    facts["tools"] = dict(zip(RELAY_TOOLS, out.decode("ascii", "replace").split()))
    return facts


def restic(args, repository, password, *command, capture=False):
    return install.checked_process([args.restic, "--repo", str(repository), "--password-file", str(password),
                                    "--no-cache", *command], 300, capture=capture)


def exercise_relay_backup(args, directory, socket_dir, booted, facts):
    """The relay half of the original service phase, against a local repository listener.

    The production service code runs over the installed host's pinned SSH and its reverse
    forward to a private Unix socket, as on a lease. The fixture then checks and restores
    the encrypted snapshot off-host. Provider retirement and a fresh-disk replacement
    restore are not part of this run.
    """
    import rehearsal_escrow as escrow

    try:
        run_relay_backup(args, directory, socket_dir, booted, facts, escrow)
    except install.Rejected as failure:
        # Our own fixed labels only (for example "relay relay-bootstrap failed").
        facts["relayBackupRejected"] = str(failure)
        raise


def run_relay_backup(args, directory, socket_dir, booted, facts, escrow):

    escrow.generate_material(directory, Path(args.openssl))
    repository = directory / "repository-data"
    repository.mkdir(mode=0o700)
    socket = socket_dir / "rest.sock"
    listener = subprocess.Popen(
        [args.rest_server, "--listen", "unix:" + str(socket), "--path", str(repository), "--no-auth",
         "--append-only", "--max-size", str(escrow.REPO_LIMIT)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not (socket.exists() and stat.S_ISSOCK(socket.lstat().st_mode)):
            install.require(listener.poll() is None and time.monotonic() < deadline, "repository listener did not start")
            time.sleep(0.2)
        relay = services.Host(directory, "relay", {"serverId": 0}, booted, install.checked_process, socket,
                              time.time() + 900)
        facts["relayBackupStep"] = "inputs"
        services.inactive(relay, services.RELAY_WRITERS)
        tls = directory / "tls"
        inputs = {name: install.read_private(tls / name, 16384) for name in ("ca.pem", "relay-cert.pem", "relay-key.pem")}
        inputs["repository-password"] = install.read_private(directory / "repository-password", 4096)
        services.install_runtime(relay, inputs)
        facts["relayBackupStep"] = "bootstrap"
        try:
            signature = services.bootstrap_relay(relay)
        except install.Rejected:
            facts["relayBootstrapDiagnostics"] = relay_diagnostics(booted)
            raise
        install.require(services.bootstrap_relay(relay) == signature, "second relay bootstrap changed identity")
        identity = relay.run("relay-identity", services.RELAY_SIGNATURE, capture=True).decode("ascii")
        # sha256sum lines of the Noise and DERP private keys; the digests stay in memory only.
        keys = {match[2]: match[1] for match in re.finditer(r"^([a-f0-9]{64})  (/var/lib/headscale/[a-z_]+[.]key)$",
                                                             identity, re.M)}
        install.require(len(keys) == 2, "relay identity keys were not listed")
        facts["relayBackupStep"] = "quiesce"
        services.quiesce_relay(relay)
        facts["relayBackupStep"] = "backup"
        relay.run("repository-init", "terrace-backup init primary", seconds=90, repository=True)
        relay.run("native-backup", "systemctl start terrace-backup.service", seconds=300, repository=True)
        services.inactive(relay, services.RELAY_WRITERS)
    finally:
        listener.terminate()
        try:
            listener.wait(timeout=10)
        except subprocess.TimeoutExpired:
            listener.kill()
            listener.wait(timeout=5)
        socket.unlink(missing_ok=True)
    facts["relayBackupStep"] = "offhost"
    stored = repository / "repository"
    password = directory / "repository-password"
    # As the service phase's off-host verifier: an encrypted config is not readable JSON.
    install.require(not install.read_private(stored / "config", 65536).lstrip().startswith(b"{"),
                    "off-host repository configuration is not encrypted")
    restic(args, stored, password, "check", "--read-data")
    snapshots = json.loads(restic(args, stored, password, "snapshots", "--json", capture=True))
    install.require(len(snapshots) == 1 and snapshots[0]["hostname"] == "relay"
                    and services.SNAPSHOT.fullmatch(snapshots[0]["id"]), "unexpected relay snapshot inventory")
    restored = directory / "restored"
    restic(args, stored, password, "restore", snapshots[0]["id"], "--target", str(restored))
    for path, digest in keys.items():
        copy = restored / path.lstrip("/")
        install.require(copy.is_file() and install.sha(copy.read_bytes()) == digest,
                        "restored relay identity key differs")
    databases = list(restored.rglob("headscale.sqlite"))
    install.require(len(databases) == 1, "restored relay database snapshot missing")
    import sqlite3
    with sqlite3.connect(f"file:{databases[0]}?mode=ro", uri=True) as connection:
        install.require(connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                        and [row[0] for row in connection.execute("select name from users")] == ["rehearsal-synthetic"],
                        "restored relay database differs")
    facts["relayBackup"] = {"idempotentBootstrap": True, "writersStopped": True, "snapshots": 1,
                            "checkReadData": True, "restoredKeys": len(keys), "restoredDatabase": True}


def exercise(args, directory, seed, overlay, facts):
    binding = {"ipv4": "127.0.0.1"}
    options = [*install.ssh_options(directory, PREFIX, "bootstrap"),
               "-o", "HostKeyAlias=" + ALIAS, "-p", str(args.port)]
    ssh = ["ssh", *options, "root@127.0.0.1"]
    wrong_options = [*install.ssh_options(directory, PREFIX, "wrong"),
                     "-o", "HostKeyAlias=" + ALIAS, "-p", str(args.port)]
    wrong = ["ssh", *wrong_options, "root@127.0.0.1"]
    nonce = (directory / f"{PREFIX}-bootstrap-nonce").read_text().strip()
    proof_target = ["-p", str(args.port), "-o", "HostKeyAlias=" + ALIAS]
    public = None
    operator = (directory / "operator-ed25519.pub").read_text().split()[1]
    serial = bytearray()
    original_remote = install.remote
    monitor = None
    late_packets = None
    try:
        with tempfile.TemporaryDirectory(prefix="terrace-kexec-qmp-", dir="/tmp") as monitor, packet_pipe(
            Path(monitor) / "headers.pipe"
        ) as packets, guest(
            args, overlay, seed, serial, facts, Path(monitor) / "qmp.sock", directory / "efi-vars.fd"
        ) as process, patch.object(
            install, "bootstrap_authority", side_effect=lambda *_args: (binding, public, options, ssh)
        ), patch.object(install, "remaining", side_effect=lambda _directory, _binding, limit: limit):
            facts["stage"] = "proof"
            public = await_proof(process, 180, nonce, proof_target)
            facts["proofPinned"] = True
            private_text(directory / f"{PREFIX}-bootstrap-known-hosts", f"{ALIAS} ssh-ed25519 {public}\n")
            for name, wrong_nonce, context in (
                ("proofWrongNonceRejected", secrets.token_hex(32), PROOF_CONTEXT),
                ("proofWrongContextRejected", nonce, (FIXTURE_RUN, "replacement", "relay")),
            ):
                try:
                    install.verified_host_key(wrong_nonce, context, "root@127.0.0.1", proof_target, 20)
                except install.Rejected:
                    facts[name] = True
                else:
                    raise install.Rejected("a wrong host key proof was accepted")
            facts["stage"] = "stock"
            try:
                await_fact(process, 150, lambda: stock_kexec.stock_boot(ssh, public, directory))
            except install.Rejected:
                facts["stockDiagnostics"] = stock_diagnostics(args.port, ssh, public, operator)
                raise
            facts["stockReady"] = True
            # A mismatched operator key must be reported at the authorized_keys comparison.
            expected = stock_kexec.STOCK_PROBE.splitlines().index('  test "$kind $key" = "ssh-ed25519 $2"') + 1
            try:
                stock_kexec.stock_check(ssh, public, public)
            except stock_kexec.StockCheckFailed as failure:
                install.require((failure.line, failure.status) == (expected, 1), "wrong stock check line reported")
            else:
                raise install.Rejected("a mismatched operator key passed the stock check")
            facts["stockFailureLineReported"] = expected
            require_fixture_ipv4(ssh, static=True)
            facts["stockStaticIpv4"] = True
            wrong_host_rejected(wrong)
            facts["stockWrongKeyRejected"] = True
            facts["stage"] = "dispatch"
            try:
                with patch.object(install, "remote", side_effect=lambda *args, **kwargs: observe_dispatch(
                    facts, serial, Path(monitor) / "qmp.sock", original_remote, *args, **kwargs
                )):
                    stock_kexec.dispatch(directory, "original", "relay", args.archive, args.archive_sha256)
            except install.Rejected:
                # The live SSH session can end during the kernel switch.
                if not (directory / f"{PREFIX}-kexec-dispatched.json").is_file():
                    raise
            sent, _ = install.read_json(directory / f"{PREFIX}-kexec-dispatched.json")
            facts["archiveBytes"] = sent["archiveBytes"]
            facts["archiveUnpackedBytes"] = sent["archiveUnpackedBytes"]
            facts["stage"] = "ram"
            try:
                # The production wait: no fixture retry around it.
                stock_kexec.verify_ram(directory, "original", "relay")
            except install.Rejected as failure:
                if isinstance(failure, stock_kexec.RamNotReady):
                    facts["ramReport"] = failure.report()
                late_packets = len(packets)
                facts["ramDiagnostics"] = ram_diagnostics(args.port, ssh, public, sent["stockBootId"])
                if not facts["ramDiagnostics"]["sshBanner"]:
                    facts["ramQmp"] = qmp_failure_facts(Path(monitor) / "qmp.sock")
                raise
            ready, _ = install.read_json(directory / f"{PREFIX}-ram-ready.json")
            facts["ramWait"] = {"waitSeconds": ready["waitSeconds"], "timeline": ready["timeline"]}
            facts["ramBoot"] = boot = ready["boot"]
            install.require(boot is not None and boot["efi"]
                            and 0 < boot["initrdStartMs"] <= boot["userspaceStartMs"] <= boot["sshdActiveMs"]
                            <= boot["uptimeSeconds"] * 1000 + 1000, "RAM boot facts are missing or out of order")
            require_fixture_ipv4(ssh, static=False)
            facts["ramIpv4Retained"] = True
            # Recorded, not required: the install's bootctl writes no firmware variables.
            code, _, _ = diagnostic_command([*ssh, "test -d /sys/firmware/efi"], 15)
            facts["ramEfi"] = code == 0
            wrong_host_rejected(wrong)
            facts["ramWrongKeyRejected"] = True
            facts["stage"] = "ram-store"
            store = args.store_probe
            install.require(install.STORE.fullmatch(store) and 1 <= Path(store).stat().st_size <= 4096,
                            "store probe is not a small Nix store file")
            install.remote(ssh, 5, 'test ! -e "$1"', store)
            install.remote(ssh, 20, install.RAM_SERVE, "430")
            try:
                install.copy_store(binding, wrong_options, 30, [store])
            except install.Rejected:
                install.remote(ssh, 5, 'test ! -e "$1"', store)
            else:
                raise install.Rejected("Nix copy accepted the wrong host key")
            facts["ramStoreWrongKeyRejected"] = True
            install.copy_store(binding, options, 30, [store])
            install.remote(ssh, 5, 'set -eu; test -f "$1"; test "$(sha256sum "$1" | cut -d " " -f1)" = "$2"',
                           store, install.sha(Path(store).read_bytes()))
            facts["ramStoreCopy"] = True
            # On a lease the install creates the store service first; leave the RAM host that way.
            install.remote(ssh, 5, 'rm -- "$1"', install.NIX_SERVE)
            facts["stage"] = "ram-target"
            exercise_target(directory, ssh, public, options, Path(monitor) / "qmp.sock", facts)
            facts["stage"] = "install"
            installed, ram_boot = exercise_install(directory, ssh, public, options, args.plan,
                                                   args.installed_public, facts)
            facts["stage"] = "installed-boot"
            booted = exercise_installed_boot(args, directory, process, Path(monitor) / "qmp.sock", ssh, installed,
                                             ram_boot, facts)
            facts["stage"] = "relay-backup"
            exercise_relay_backup(args, directory, Path(monitor), booted, facts)
    finally:
        if facts.get("packetCaptureStarted"):
            facts["packetFacts"] = packet_facts(packets, late_packets)
        if monitor is not None:
            facts["qmpScratchGone"] = not Path(monitor).exists()
            install.require(facts["qmpScratchGone"], "private monitor scratch survived cleanup")
        forbidden = {b"PRIVATE KEY-----"}
        for name in ("operator-ed25519", f"{PREFIX}-installed-ed25519"):
            if (directory / name).exists():
                forbidden.update((directory / name).read_bytes().splitlines()[1:-1])
        facts["serialPrivateKeyFound"] = any(marker in serial for marker in forbidden if marker)
        lower_serial = serial.lower()
        facts["serialBootMarkers"] = {
            name: any(marker.lower() in lower_serial for marker in markers)
            for name, markers in SERIAL_MARKERS.items()
        }
        if "postDispatchSerialStart" in facts:
            tail = lower_serial[facts["postDispatchSerialStart"]:]
            facts["postDispatchSerial"] = {
                "bytes": len(tail), "kernelStarts": tail.count(b"linux version "),
                **{name: any(marker.lower() in tail for marker in markers)
                   for name, markers in RAM_SERIAL_MARKERS.items()},
                "unitFacts": boot_unit_facts(tail),
            }
        facts["serialBytesCaptured"] = len(serial)
        install.require(not facts["serialPrivateKeyFound"], "guest serial contained private key material")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--store-probe", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--install-source", type=Path, required=True,
                        help="a checkout of this revision; the relay's install outputs are built from it")
    for tool in ("qemu", "qemu-img", "mkfs-vfat", "mcopy", "ovmf-code", "ovmf-vars", "openssl", "rest-server",
                 "restic"):
        parser.add_argument("--" + tool, required=True)
    args = parser.parse_args()
    facts = {"stage": "inputs"}
    try:
        output = install.private_directory(args.output)
        install.require(1024 <= args.port <= 65535 and "," not in str(output), "unsafe fixture transport")
        install.require(base_digest(args.base) == BASE_SHA512, "Debian base digest differs")
        with tempfile.TemporaryDirectory(prefix="guest-", dir=output) as scratch:
            directory = Path(scratch)
            nonce = secrets.token_hex(32)
            private_text(directory / f"{PREFIX}-bootstrap-nonce", nonce + "\n")
            operator_public = key(directory / "operator-ed25519")
            args.installed_public = key(directory / f"{PREFIX}-installed-ed25519").split()[1]
            facts["stage"] = "install-build"
            start = time.monotonic()
            args.plan = build_install_plan(args, directory, operator_public)
            facts["installBuildSeconds"] = round(time.monotonic() - start)
            wrong_public = key(directory / "wrong-ed25519")
            private_text(directory / f"{PREFIX}-wrong-known-hosts", f"{ALIAS} {wrong_public}\n")
            seed = directory / "seed.img"
            render_seed(args, directory, seed, nonce, operator_public)
            overlay = directory / "guest.qcow2"
            template = Path(args.ovmf_vars).read_bytes()
            install.require(0 < len(template) <= 8 * 1024 * 1024, "unexpected OVMF variable store")
            with os.fdopen(os.open(directory / "efi-vars.fd", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                           "wb") as stream:
                stream.write(template)
            install.checked_process([args.qemu_img, "create", "-f", "qcow2", "-F", "qcow2",
                                     "-b", str(args.base.resolve()), str(overlay), f"{FIXTURE_DISK_GB}G"], 10)
            try:
                exercise(args, directory, seed, overlay, facts)
            finally:
                facts["baseUnchanged"] = base_digest(args.base) == BASE_SHA512
                install.require(facts["baseUnchanged"], "Debian base changed")
            facts["stage"] = "complete"
        install.write_private(output / "stock-kexec-native.json", facts)
        print("Strict SSH handoff, RAM install and installed boot passed. The offline guest is off.")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError):
        if "output" in locals():
            install.write_private(output / "stock-kexec-native-failure.json", facts)
        print("Offline stock kexec proof rejected. Private inputs suppressed.", file=sys.stderr)
        return 1
    return 0


def stop(_signum, _frame):
    raise InterruptedError("offline proof interrupted")


if __name__ == "__main__":
    os.umask(0o077)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    sys.exit(main())
