"""Prove the strict stock-to-RAM SSH handoff with one isolated Debian guest."""

import argparse
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
FIXTURE_MAC = "52:54:00:12:34:56"
FIXTURE_IP = socket.inet_ntoa(EXPECTED)
SERIAL_LIMIT = 256 * 1024
SERIAL_MARKERS = {
    "seaBios": (b"SeaBIOS",),
    "firmwareDiskBoot": (b"Booting from Hard Disk",),
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
    install.checked_process([args.mkfs_vfat, "-n", "CIDATA", str(seed)], 15)
    install.checked_process(
        [args.mcopy, "-i", str(seed), str(data), str(metadata), str(network), "::/"], 15
    )


@contextmanager
def guest(args, overlay, seed, serial, facts, qmp):
    command = [
        args.qemu, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "4096", "-smp", "2",
        "-nodefaults", "-boot", "order=c", "-vga", "std", "-display", "none",
        "-monitor", "none", "-serial", "stdio",
        "-qmp", f"unix:{qmp},server=on,wait=off",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-drive", f"file={seed},format=raw,if=virtio,readonly=on",
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
            if line <= len(lines) and "cloud" in lines[line - 1]:
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
            args, overlay, seed, serial, facts, Path(monitor) / "qmp.sock"
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
                await_fact(process, 150, lambda: stock_kexec.verify_ram(directory, "original", "relay"))
            except install.Rejected:
                late_packets = len(packets)
                facts["ramDiagnostics"] = ram_diagnostics(args.port, ssh, public, sent["stockBootId"])
                if not facts["ramDiagnostics"]["sshBanner"]:
                    facts["ramQmp"] = qmp_failure_facts(Path(monitor) / "qmp.sock")
                raise
            require_fixture_ipv4(ssh, static=False)
            facts["ramIpv4Retained"] = True
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
    finally:
        if facts.get("packetCaptureStarted"):
            facts["packetFacts"] = packet_facts(packets, late_packets)
        if monitor is not None:
            facts["qmpScratchGone"] = not Path(monitor).exists()
            install.require(facts["qmpScratchGone"], "private monitor scratch survived cleanup")
        operator = (directory / "operator-ed25519").read_bytes()
        forbidden = {b"PRIVATE KEY-----", *operator.splitlines()[1:-1]}
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
    for tool in ("qemu", "qemu-img", "mkfs-vfat", "mcopy"):
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
            wrong_public = key(directory / "wrong-ed25519")
            private_text(directory / f"{PREFIX}-wrong-known-hosts", f"{ALIAS} {wrong_public}\n")
            seed = directory / "seed.img"
            render_seed(args, directory, seed, nonce, operator_public)
            overlay = directory / "guest.qcow2"
            install.checked_process([args.qemu_img, "create", "-f", "qcow2", "-F", "qcow2",
                                     "-b", str(args.base.resolve()), str(overlay)], 10)
            try:
                exercise(args, directory, seed, overlay, facts)
            finally:
                facts["baseUnchanged"] = base_digest(args.base) == BASE_SHA512
                install.require(facts["baseUnchanged"], "Debian base changed")
            facts["stage"] = "complete"
        install.write_private(output / "stock-kexec-native.json", facts)
        print("Strict SSH handoff and RAM store copy passed. The offline guest is off.")
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
