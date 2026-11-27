"""Throwaway SFTP restic repository for the production lease host, confined by construction.

The production backup module accepts only an `sftp:` repository with a pinned known-hosts entry. This helper serves
one on the build machine for the length of a lease. The server is `rclone serve sftp` (the pinned nixpkgs package):
a userspace SFTP server whose only filesystem view is one directory, with public-key authentication only, one
ed25519 host key and an authorized-keys file holding exactly one key. It has no shell, no exec of arbitrary
commands, no port forwarding and no chroot-less path escape: every path is resolved inside the served root, and the
local backend does not follow symlinks. The transient user unit adds a second layer (a mount namespace in which the
run directory shows only the repository, the host key and the authorized keys). No sudo; nothing outside the run
directory is written; the unit has a hard runtime limit.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import rehearsal_lease as lease


SYSTEMD = Path("/run/current-system/sw/bin")
HOST_KEY, HOST_PUB, AUTHORIZED, REPOSITORY, LOGS = "sftp-host-key", "sftp-host-key.pub", "sftp-authorized-keys", "app", "sftp-logs"
RECEIPT, STOPPED = "sftp-listener.json", "sftp-stopped.json"
ADDRESS = "127.0.0.1"
KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{68}")
RCLONE = re.compile(r"/nix/store/[0-9a-z]{32}-rclone-[0-9.]+/bin/rclone")
KEYGEN = re.compile(r"/nix/store/[0-9a-z]{32}-openssh-[0-9.]+p[0-9]+/bin/ssh-keygen")
MIN_SECONDS, MAX_SECONDS = 300, 21600
UNIT = re.compile(r"terrace-lease-sftp-[0-9a-f]{16}\.service")
SERVED = "/var/empty"
PATH = re.compile(r"/[A-Za-z0-9/._-]+")


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or not PATH.fullmatch(str(path)):
        raise lease.Rejected("unsafe path")
    return str(path)


def put_bytes(destination, content):
    """Create `destination` with mode 0600, refusing to replace anything (hard link, never rename)."""
    with tempfile.NamedTemporaryFile(dir=destination.parent) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        os.link(output.name, destination)


def read_public_key(path):
    """One `ssh-ed25519 <key> [comment]` line; anything else (a private key, two keys) is rejected."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(2049)
    if not 1 <= len(data) <= 2048:
        raise lease.Rejected("public key file size rejected")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise lease.Rejected("public key rejected") from None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or "PRIVATE" in text or not re.fullmatch(KEY.pattern + r"( [^\x00-\x1f\x7f]*)?", lines[0]):
        raise lease.Rejected("public key rejected")
    return lines[0][:len("ssh-ed25519 ") + 68]


def run_names(directory):
    return [directory / name for name in (HOST_KEY, HOST_PUB, AUTHORIZED, REPOSITORY, LOGS, RECEIPT, STOPPED)]


def prepare(directory, keygen, public_key_file):
    """Create the host key, the authorized-keys file and an empty repository directory; refuse existing files."""
    if not KEYGEN.fullmatch(str(keygen)) or not keygen.is_file():
        raise lease.Rejected("pinned ssh-keygen required")
    if any(os.path.lexists(path) for path in run_names(directory)):
        raise lease.Rejected("run directory already initialized")
    client = read_public_key(public_key_file)
    with tempfile.TemporaryDirectory(prefix="hostkey-", dir=directory) as name:
        work = Path(name)
        subprocess.run([str(keygen), "-q", "-t", "ed25519", "-N", "", "-C", "terrace-lease-sftp-host", "-f", str(work / "key")],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
        private, public = (work / "key").read_bytes(), (work / "key.pub").read_bytes()
        host = public.decode("ascii").split()
        if len(host) < 2 or not KEY.fullmatch(host[0] + " " + host[1]):
            raise lease.Rejected("generated host key rejected")
        put_bytes(directory / HOST_KEY, private)
        put_bytes(directory / HOST_PUB, (host[0] + " " + host[1] + "\n").encode())
    put_bytes(directory / AUTHORIZED, (client + "\n").encode())
    (directory / REPOSITORY).mkdir(mode=0o700)
    (directory / LOGS).mkdir(mode=0o700)


def host_key_line(directory):
    text = lease.read_private(directory / HOST_PUB, 256).decode("ascii").strip()
    if not KEY.fullmatch(text):
        raise lease.Rejected("host key rejected")
    return text


def systemctl(*args, allow_inactive=False):
    result = subprocess.run([str(SYSTEMD / "systemctl"), "--user", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"), text=True, timeout=30, check=False)
    if result.returncode and not allow_inactive:
        raise lease.Rejected("systemd command failed")
    return result.stdout.strip()


def properties(unit, keys):
    output = systemctl("show", unit, *("--property=" + key for key in keys), allow_inactive=True)
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def free_port():
    with socket.socket() as probe:
        probe.bind((ADDRESS, 0))
        port = probe.getsockname()[1]
    if port < 1024:
        raise lease.Rejected("port rejected")
    return port


def banner(port, timeout=2.0):
    """The SSH identification string if something answers on the loopback port, else None."""
    try:
        with socket.create_connection((ADDRESS, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            return connection.recv(64).startswith(b"SSH-2.0-")
    except OSError:
        return None


def unit_properties(directory, unit, rclone, port, seconds):
    run = directory.resolve(strict=True)
    repository, logs = run / REPOSITORY, run / LOGS
    return [
        "--property=Type=exec", "--property=RuntimeMaxSec=" + str(seconds), "--property=TimeoutStopSec=10s",
        "--property=KillMode=control-group", "--property=UMask=0077", "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes", "--property=PrivateDevices=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs", "--property=ProtectKernelTunables=yes", "--property=ProtectControlGroups=yes",
        "--property=RestrictAddressFamilies=AF_INET AF_UNIX", "--property=MemoryMax=1G", "--property=TasksMax=256",
        "--property=LimitNOFILE=4096",
        # The run directory shows only what the server needs: the host key and keys read-only, the log writable, and the
        # repository as the single writable entry in an otherwise empty read-only served root (/var/empty/app, so the
        # client's relative path `app` is the repository and it cannot create anything beside it).
        "--property=TemporaryFileSystem=" + safe_path(run) + ":ro " + SERVED + ":ro",
        "--property=BindReadOnlyPaths=" + " ".join(safe_path(run / name) for name in (HOST_KEY, AUTHORIZED)),
        "--property=BindPaths=" + safe_path(logs) + " " + safe_path(repository) + ":" + SERVED + "/app",
        "--property=WorkingDirectory=" + SERVED,
    ]


def server_command(directory, rclone, port):
    run = directory.resolve(strict=True)
    return [safe_path(rclone), "serve", "sftp", ":local:" + SERVED,
            "--addr", "%s:%d" % (ADDRESS, port), "--key", safe_path(run / HOST_KEY),
            "--authorized-keys", safe_path(run / AUTHORIZED), "--config", "/dev/null",
            "--log-file", safe_path(run / LOGS / "rclone.log"), "--log-level", "INFO", "--vfs-cache-mode", "off",
            "--no-modtime", "--umask", "077"]


def start(directory, rclone, seconds, port=None):
    if not RCLONE.fullmatch(str(rclone)) or not rclone.is_file():
        raise lease.Rejected("pinned rclone required")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise lease.Rejected("runtime limit rejected")
    if os.path.lexists(directory / RECEIPT) or os.path.lexists(directory / STOPPED):
        raise lease.Rejected("endpoint already started in this run directory")
    for name in (HOST_KEY, HOST_PUB, AUTHORIZED):
        lease.read_private(directory / name, 4096)
    host = host_key_line(directory)
    for name in (REPOSITORY, LOGS):
        if not (directory / name).is_dir() or (directory / name).is_symlink():
            raise lease.Rejected("run directory not prepared")
    port = free_port() if port is None else port
    if not 1024 <= port <= 65535 or banner(port, 0.5) is not None:
        raise lease.Rejected("port unavailable")
    unit = "terrace-lease-sftp-" + secrets.token_hex(8) + ".service"
    if properties(unit, ["LoadState"]).get("LoadState") != "not-found":
        raise lease.Rejected("unit name in use")
    try:
        result = subprocess.run([str(SYSTEMD / "systemd-run"), "--user", "--quiet", "--collect", "--unit=" + unit,
                                 *unit_properties(directory, unit, rclone, port, seconds), *server_command(directory, rclone, port)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(os.environ, LC_ALL="C"), timeout=30, check=False)
        if result.returncode:
            raise lease.Rejected("unit did not start")
        for _ in range(40):
            if banner(port, 0.5):
                break
            time.sleep(0.25)
        else:
            raise lease.Rejected("SFTP listener unavailable")
        facts = properties(unit, ["MainPID", "ActiveState"])
        if facts.get("ActiveState") != "active" or not facts.get("MainPID", "0").isdigit() or facts["MainPID"] == "0":
            raise lease.Rejected("unit not active")
        receipt = {"schema": 1, "unit": unit, "pid": int(facts["MainPID"]), "address": ADDRESS, "port": port,
                   "hostKey": host, "knownHosts": "[%s]:%d %s" % (ADDRESS, port, host), "repository": "sftp:terrace@%s:app" % ADDRESS,
                   "started": int(time.time()), "expires": int(time.time()) + seconds}
        lease.write_private(directory / RECEIPT, receipt)
        return receipt
    except BaseException:
        systemctl("stop", unit, allow_inactive=True)
        raise


def read_receipt(directory):
    receipt = json.loads(lease.read_private(directory / RECEIPT, 64 * 1024))
    if (receipt["schema"] != 1 or not UNIT.fullmatch(receipt["unit"]) or receipt["address"] != ADDRESS
            or not isinstance(receipt["port"], int) or not 1024 <= receipt["port"] <= 65535):
        raise lease.Rejected("receipt rejected")
    return receipt


def directory_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def status(directory):
    receipt = read_receipt(directory)
    facts = properties(receipt["unit"], ["ActiveState", "SubState", "MainPID", "LoadState"])
    return {"unit": receipt["unit"], "active": facts.get("ActiveState") == "active", "state": facts.get("ActiveState"),
            "pid": int(facts.get("MainPID", "0") or 0), "port": receipt["port"], "listening": bool(banner(receipt["port"])),
            "expires": receipt["expires"], "repositoryBytes": directory_size(directory / REPOSITORY)}


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(directory):
    """Stop the unit, then prove the unit, its processes and the listener are gone."""
    receipt = read_receipt(directory)
    unit = receipt["unit"]
    systemctl("stop", unit, allow_inactive=True)
    facts = properties(unit, ["ActiveState", "MainPID", "ControlPID", "ControlGroup"])
    if (facts.get("ActiveState", "inactive") not in ("inactive", "failed") or facts.get("MainPID", "0") != "0"
            or facts.get("ControlPID", "0") != "0" or facts.get("ControlGroup", "")):
        raise lease.Rejected("unit is not inactive")
    if receipt["pid"] and pid_alive(receipt["pid"]):
        cmdline = Path("/proc/%d/cmdline" % receipt["pid"])
        try:
            if str(receipt["port"]).encode() in cmdline.read_bytes():
                raise lease.Rejected("server process still running")
        except OSError:
            pass
    if banner(receipt["port"], 1.0) is not None:
        raise lease.Rejected("listener still answering")
    if not os.path.lexists(directory / STOPPED):
        lease.write_private(directory / STOPPED, {"unit": unit, "port": receipt["port"], "stopped": int(time.time())})


def forward_option(directory, host_port=None):
    receipt = read_receipt(directory)
    host_port = receipt["port"] if host_port is None else host_port
    if not 1024 <= host_port <= 65535:
        raise lease.Rejected("port rejected")
    return "-R %s:%d:%s:%d" % (ADDRESS, host_port, ADDRESS, receipt["port"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "status", "stop", "forward"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--ssh-keygen", type=Path)
    parser.add_argument("--rclone", type=Path)
    parser.add_argument("--client-public-key", type=Path, help="file holding the one ssh-ed25519 public key allowed to connect")
    parser.add_argument("--seconds", type=int, default=14400, help="hard runtime limit, 300 to 21600")
    parser.add_argument("--port", type=int, help="loopback port on this machine (default: a free one)")
    parser.add_argument("--host-port", type=int, help="loopback port on the lease host for `forward` (default: same as --port)")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        directory = lease.private_directory(args.directory)
        if args.action == "prepare" and args.ssh_keygen and args.client_public_key:
            prepare(directory, args.ssh_keygen, args.client_public_key)
            print(json.dumps({"hostKey": host_key_line(directory)}, sort_keys=True))
        elif args.action == "start" and args.rclone:
            receipt = start(directory, args.rclone, args.seconds, args.port)
            print(json.dumps({key: receipt[key] for key in ("unit", "pid", "port", "hostKey", "knownHosts", "repository", "expires")}, sort_keys=True))
        elif args.action == "status":
            print(json.dumps(status(directory), sort_keys=True))
        elif args.action == "stop":
            stop(directory)
            print("Throwaway SFTP endpoint stopped; unit, processes and listener verified gone.")
        elif args.action == "forward":
            print(forward_option(directory, args.host_port))
        else:
            raise lease.Rejected("required arguments missing")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Throwaway SFTP operation rejected. Private output suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
