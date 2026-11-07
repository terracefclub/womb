"""Prove the strict stock-to-RAM SSH handoff with one isolated Debian guest."""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import rescue_install as install
import stock_kexec


BASE_SHA512 = (
    "d25999de0770d4cde1be72c299ba3ad94eac7ea8fd4d18f8cc86c69df69337e2e8"
    "a91d64465a9bfd8f2a78c566fcbcb0842eacc91c09efdef72d719d9f13b535"
)
ALIAS = "terrace-stock-kexec-fixture"
PREFIX = "original-relay"
SERIAL_LIMIT = 256 * 1024


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


def render_seed(args, directory, seed, stock_public, operator_public):
    renderer = Path(__file__).resolve().parents[1] / "cloud/rehearsal-create/bootstrap.ts"
    script = """
import { pathToFileURL } from "node:url";
const [renderer, privateKeyPath, hostPublicKey, operatorPublicKey] = process.argv.slice(1);
const { renderBootstrapCloudConfig } = await import(pathToFileURL(renderer).href);
const hostPrivateKey = await Bun.file(privateKeyPath).text();
process.stdout.write(renderBootstrapCloudConfig(hostPublicKey, hostPrivateKey, operatorPublicKey));
"""
    user_data = install.checked_process(
        ["bun", "-e", script, str(renderer), str(directory / "stock-ed25519"),
         stock_public, operator_public], 10, capture=True
    ).decode("utf-8")
    install.require(user_data.startswith("#cloud-config\n"), "creator did not return cloud config")
    data = directory / "user-data"
    metadata = directory / "meta-data"
    private_text(data, user_data)
    private_text(metadata, f"instance-id: {ALIAS}\nlocal-hostname: {ALIAS}\n")
    with seed.open("xb") as stream:
        stream.truncate(2 * 1024 * 1024)
    install.checked_process([args.mkfs_vfat, "-n", "CIDATA", str(seed)], 15)
    install.checked_process(
        [args.mcopy, "-i", str(seed), str(data), str(metadata), "::/"], 15
    )


@contextmanager
def guest(args, overlay, seed, serial, facts):
    command = [
        args.qemu, "-machine", "q35,accel=kvm", "-cpu", "host", "-m", "4096", "-smp", "2",
        "-nodefaults", "-boot", "order=c", "-display", "none", "-monitor", "none", "-serial", "stdio",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-drive", f"file={seed},format=raw,if=virtio,readonly=on",
        "-nic", f"user,model=virtio-net-pci,restrict=on,hostfwd=tcp:127.0.0.1:{args.port}-:22",
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


def exercise(args, directory, seed, overlay, facts):
    binding = {"ipv4": "127.0.0.1"}
    options = [*install.ssh_options(directory, PREFIX, "bootstrap"),
               "-o", "HostKeyAlias=" + ALIAS, "-p", str(args.port)]
    ssh = ["ssh", *options, "root@127.0.0.1"]
    wrong = ["ssh", *install.ssh_options(directory, PREFIX, "wrong"),
             "-o", "HostKeyAlias=" + ALIAS, "-p", str(args.port), "root@127.0.0.1"]
    public = (directory / "stock-ed25519.pub").read_text().split()[1]
    serial = bytearray()
    try:
        with guest(args, overlay, seed, serial, facts) as process, patch.object(
            install, "bootstrap_authority", return_value=(binding, public, options, ssh)
        ), patch.object(install, "remaining", side_effect=lambda _directory, _binding, limit: limit):
            facts["stage"] = "stock"
            await_fact(process, 150, lambda: stock_kexec.stock_boot(ssh, public, directory))
            wrong_host_rejected(wrong)
            facts["stockWrongKeyRejected"] = True
            facts["stage"] = "dispatch"
            try:
                stock_kexec.dispatch(directory, "original", "relay", args.archive, args.archive_sha256)
            except install.Rejected:
                # The live SSH session can end during the kernel switch.
                if not (directory / f"{PREFIX}-kexec-dispatched.json").is_file():
                    raise
            facts["stage"] = "ram"
            await_fact(process, 150, lambda: stock_kexec.verify_ram(directory, "original", "relay"))
            wrong_host_rejected(wrong)
            facts["ramWrongKeyRejected"] = True
            facts["stage"] = "ram-store"
            store = args.store_probe
            install.require(install.STORE.fullmatch(store) and 1 <= Path(store).stat().st_size <= 4096,
                            "store probe is not a small Nix store file")
            install.remote(ssh, 5, 'test ! -e "$1"', store)
            install.remote(ssh, 20, install.RAM_SERVE, "430")
            install.copy_store(binding, options, 30, [store])
            install.remote(ssh, 5, 'set -eu; test -f "$1"; test "$(sha256sum "$1" | cut -d " " -f1)" = "$2"',
                           store, install.sha(Path(store).read_bytes()))
            facts["ramStoreCopy"] = True
    finally:
        private = (directory / "stock-ed25519").read_bytes()
        operator = (directory / "operator-ed25519").read_bytes()
        forbidden = {b"PRIVATE KEY-----", *private.splitlines()[1:-1], *operator.splitlines()[1:-1]}
        facts["serialPrivateKeyFound"] = any(marker in serial for marker in forbidden if marker)
        facts["serialKernelPanic"] = b"Kernel panic" in serial
        facts["serialCloudInit"] = b"Cloud-init v" in serial
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
            stock_public = key(directory / "stock-ed25519")
            operator_public = key(directory / "operator-ed25519")
            wrong_public = key(directory / "wrong-ed25519")
            private_text(directory / f"{PREFIX}-bootstrap-known-hosts", f"{ALIAS} {stock_public}\n")
            private_text(directory / f"{PREFIX}-wrong-known-hosts", f"{ALIAS} {wrong_public}\n")
            seed = directory / "seed.img"
            render_seed(args, directory, seed, stock_public, operator_public)
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
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
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
