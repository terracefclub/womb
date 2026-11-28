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
import hashlib
import json
import os
from pathlib import Path
import re
import signal
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
# The endpoint is needed from the backup step to teardown of a lease planned at about 90 minutes and capped at 4 hours.
MIN_SECONDS, DEFAULT_SECONDS, MAX_SECONDS = 300, 7200, 14400
DEFAULT_LIMIT_MIB, MIN_LIMIT_MIB, MAX_LIMIT_MIB = 2048, 16, 65536
# Every file and directory counts at least one filesystem block against the size limit, and the number of entries is capped.
ENTRY_BYTES, DEFAULT_MAX_ENTRIES, MIN_MAX_ENTRIES, MAX_MAX_ENTRIES = 4096, 200000, 1000, 5000000
WATCH_INTERVAL, WATCH_GRACE = 2, 60
PROC = Path("/proc")
TRIPPED = "sftp-limit-tripped.json"
UNIT = re.compile(r"terrace-lease-sftp-[0-9a-f]{16}(-watch)?\.service")
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
    return [directory / name for name in (HOST_KEY, HOST_PUB, AUTHORIZED, REPOSITORY, LOGS, RECEIPT, STOPPED, TRIPPED)]


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


def server_properties(directory, seconds, watcher):
    run = directory.resolve(strict=True)
    repository, logs = run / REPOSITORY, run / LOGS
    return [
        "--property=Type=exec", "--property=RuntimeMaxSec=" + str(seconds), "--property=TimeoutStopSec=10s",
        "--property=KillMode=control-group", "--property=UMask=0077", "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes", "--property=PrivateDevices=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs", "--property=ProtectKernelTunables=yes", "--property=ProtectControlGroups=yes",
        "--property=RestrictAddressFamilies=AF_INET AF_UNIX", "--property=MemoryMax=1G", "--property=TasksMax=256",
        "--property=LimitNOFILE=4096",
        # The server cannot outlive its size watcher (the watcher also ends when the server does).
        "--property=BindsTo=" + watcher, "--property=After=" + watcher,
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
    # Errors only, and the log rotates at 5M keeping two old files, so client activity cannot grow it without bound.
    return [safe_path(rclone), "serve", "sftp", ":local:" + SERVED,
            "--addr", "%s:%d" % (ADDRESS, port), "--key", safe_path(run / HOST_KEY),
            "--authorized-keys", safe_path(run / AUTHORIZED), "--config", "/dev/null",
            "--log-file", safe_path(run / LOGS / "rclone.log"), "--log-level", "ERROR",
            "--log-file-max-size", "5M", "--log-file-max-backups", "2", "--vfs-cache-mode", "off",
            "--no-modtime", "--umask", "077"]


def watcher_properties(directory, seconds):
    run = directory.resolve(strict=True)
    return [
        "--property=Type=exec", "--property=RuntimeMaxSec=" + str(seconds), "--property=TimeoutStopSec=10s",
        "--property=KillMode=control-group", "--property=UMask=0077", "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes", "--property=PrivateDevices=yes", "--property=ProtectSystem=strict",
        "--property=ProtectHome=read-only", "--property=ReadWritePaths=" + safe_path(run),
        "--property=RestrictAddressFamilies=AF_UNIX", "--property=MemoryMax=256M", "--property=TasksMax=32",
    ]


def watcher_command(directory, unit, limit_bytes, max_entries):
    python = Path(sys.executable).resolve(strict=True)
    script = Path(__file__).resolve(strict=True)
    return [safe_path(python), "-B", safe_path(script), "watch", "--directory", safe_path(directory.resolve(strict=True)),
            "--unit", unit, "--limit-bytes", str(limit_bytes), "--max-entries", str(max_entries), "--interval", str(WATCH_INTERVAL)]


def usage(directory):
    """(bytes, entries) held by the repository and the log. Each file and directory counts at least ENTRY_BYTES, so
    empty files and directories use up the limit too."""
    total = entries = 0
    for name in (REPOSITORY, LOGS):
        for root, directories, files in os.walk(directory / name):
            for entry in directories + files:
                entries += 1
                try:
                    total += max(os.lstat(os.path.join(root, entry)).st_size, ENTRY_BYTES)
                except OSError:
                    total += ENTRY_BYTES
    return total, entries


def watch(directory, unit, limit_bytes, max_entries, interval, sleep=time.sleep):
    """Stop the server unit as soon as the repository plus log exceed the size or entry limit; end when the server has ended."""
    if not UNIT.fullmatch(unit) or unit.endswith("-watch.service") or limit_bytes <= 0 or max_entries <= 0:
        raise lease.Rejected("watch arguments rejected")
    seen, waited = False, 0
    while True:
        if properties(unit, ["ActiveState"]).get("ActiveState") in ("active", "activating"):
            seen = True
        elif seen or waited >= WATCH_GRACE:
            return False
        used, entries = usage(directory)
        if used > limit_bytes or entries > max_entries:
            lease.write_private(directory / TRIPPED, {"limitBytes": limit_bytes, "usedBytes": used, "maxEntries": max_entries,
                                                      "entries": entries, "at": int(time.time())})
            systemctl("stop", unit, allow_inactive=True)
            return True
        sleep(interval)
        waited += interval


def check_namespace(pid, directory, proc=PROC):
    """Through /proc/<pid>/root, the server must see exactly the repository in its root and exactly the three expected
    entries where the run directory is. Anything else, or anything unreadable, is a failure."""
    root = proc / str(pid) / "root"
    run = directory.resolve(strict=True)
    try:
        served = sorted(os.listdir(root / SERVED.lstrip("/")))
        visible = sorted(os.listdir(root / str(run).lstrip("/")))
        repository = (root / SERVED.lstrip("/") / "app")
        ok = served == ["app"] and repository.is_dir() and not repository.is_symlink() and visible == sorted([HOST_KEY, AUTHORIZED, LOGS])
    except OSError:
        ok = False
    if not ok:
        raise lease.Rejected("sandbox layout check failed")


def unit_names(directory):
    """This run's two unit names, derived from its run directory: a caller that knows only the directory (no receipt,
    for example after an interrupted `start`) can stop exactly these and never another run's."""
    tag = hashlib.sha256(os.path.realpath(directory).encode()).hexdigest()[:16]
    return f"terrace-lease-sftp-{tag}.service", f"terrace-lease-sftp-{tag}-watch.service"


def stop_on_signal(_signum, _frame):
    raise KeyboardInterrupt


def install_signal_handlers():
    """SIGTERM and SIGHUP end this process like SIGINT does, so an interrupted `start` still runs its clean-up (its
    `except BaseException` stops whatever it launched). Without this a TERM ends the process at once, the units it
    launched run on to their runtime limit, and with no receipt yet `stop` cannot name them."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop_on_signal)


def start(directory, rclone, seconds, port=None, limit_mib=DEFAULT_LIMIT_MIB, max_entries=DEFAULT_MAX_ENTRIES):
    if not RCLONE.fullmatch(str(rclone)) or not rclone.is_file():
        raise lease.Rejected("pinned rclone required")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS or not MIN_LIMIT_MIB <= limit_mib <= MAX_LIMIT_MIB \
            or not MIN_MAX_ENTRIES <= max_entries <= MAX_MAX_ENTRIES:
        raise lease.Rejected("runtime or size limit rejected")
    if os.path.lexists(directory / RECEIPT) or os.path.lexists(directory / STOPPED) or os.path.lexists(directory / TRIPPED):
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
    unit, watcher = unit_names(directory)
    if properties(unit, ["LoadState"]).get("LoadState") != "not-found":
        raise lease.Rejected("unit name in use")
    limit_bytes = limit_mib * 1024 * 1024

    def launch(name, props, command):
        result = subprocess.run([str(SYSTEMD / "systemd-run"), "--user", "--quiet", "--collect", "--unit=" + name, *props, *command],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(os.environ, LC_ALL="C"), timeout=30, check=False)
        if result.returncode:
            raise lease.Rejected("unit did not start")

    try:
        launch(watcher, watcher_properties(directory, seconds), watcher_command(directory, unit, limit_bytes, max_entries))
        launch(unit, server_properties(directory, seconds, watcher), server_command(directory, rclone, port))
        for _ in range(40):
            if banner(port, 0.5):
                break
            time.sleep(0.25)
        else:
            raise lease.Rejected("SFTP listener unavailable")
        facts = properties(unit, ["MainPID", "ActiveState"])
        if facts.get("ActiveState") != "active" or not facts.get("MainPID", "0").isdigit() or facts["MainPID"] == "0":
            raise lease.Rejected("unit not active")
        if properties(watcher, ["ActiveState"]).get("ActiveState") != "active":
            raise lease.Rejected("size watcher not active")
        check_namespace(int(facts["MainPID"]), directory)
        now = int(time.time())
        receipt = {"schema": 1, "unit": unit, "watcher": watcher, "pid": int(facts["MainPID"]), "address": ADDRESS, "port": port,
                   "hostKey": host, "knownHosts": "[%s]:%d %s" % (ADDRESS, port, host), "repository": "sftp:terrace@%s:app" % ADDRESS,
                   "started": now, "expires": now + seconds, "limitBytes": limit_bytes, "maxEntries": max_entries}
        lease.write_private(directory / RECEIPT, receipt)
        return receipt
    except BaseException:
        systemctl("stop", unit, watcher, allow_inactive=True)
        # Re-verify before returning the error: a unit that is still running must not be mistaken for a clean failure.
        for name in (unit, watcher):
            if properties(name, ["ActiveState"]).get("ActiveState", "inactive") not in ("inactive", "failed"):
                raise lease.Rejected("units still running after a failed start")
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
    counted = usage(directory)
    tripped = None
    if os.path.lexists(directory / TRIPPED):
        tripped = json.loads(lease.read_private(directory / TRIPPED, 4096))
    return {"unit": receipt["unit"], "active": facts.get("ActiveState") == "active", "state": facts.get("ActiveState"),
            "pid": int(facts.get("MainPID", "0") or 0), "port": receipt["port"], "listening": bool(banner(receipt["port"])),
            "expires": receipt["expires"], "repositoryBytes": directory_size(directory / REPOSITORY),
            "logBytes": directory_size(directory / LOGS), "countedBytes": counted[0], "entries": counted[1],
            "limitBytes": receipt.get("limitBytes"), "maxEntries": receipt.get("maxEntries"), "limitTripped": tripped}


def server_running(pid, port, proc=None):
    """True if /proc/<pid> still holds the server (its command line names the port), whatever systemd says."""
    try:
        cmdline = ((PROC if proc is None else proc) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    return b"rclone" in cmdline and ("127.0.0.1:%d" % port).encode() in cmdline


def stop(directory):
    """Stop the server and its size watcher, then prove the units, the process and the listener are gone."""
    receipt = read_receipt(directory)
    units = [receipt["unit"]] + ([receipt["watcher"]] if receipt.get("watcher") else [])
    if not all(UNIT.fullmatch(unit) for unit in units):
        raise lease.Rejected("receipt rejected")
    systemctl("stop", *units, allow_inactive=True)
    for unit in units:
        facts = properties(unit, ["ActiveState", "MainPID", "ControlPID", "ControlGroup"])
        if (facts.get("ActiveState", "inactive") not in ("inactive", "failed") or facts.get("MainPID", "0") != "0"
                or facts.get("ControlPID", "0") != "0" or facts.get("ControlGroup", "")):
            raise lease.Rejected("unit is not inactive")
    if receipt["pid"] and server_running(receipt["pid"], receipt["port"]):
        raise lease.Rejected("server process still running")
    if banner(receipt["port"], 1.0) is not None:
        raise lease.Rejected("listener still answering")
    if not os.path.lexists(directory / STOPPED):
        lease.write_private(directory / STOPPED, {"unit": receipt["unit"], "port": receipt["port"], "stopped": int(time.time())})


def forward_option(directory, host_port=None):
    receipt = read_receipt(directory)
    host_port = receipt["port"] if host_port is None else host_port
    if not 1024 <= host_port <= 65535:
        raise lease.Rejected("port rejected")
    return "-R %s:%d:%s:%d" % (ADDRESS, host_port, ADDRESS, receipt["port"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "status", "stop", "forward", "watch", "units"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--ssh-keygen", type=Path)
    parser.add_argument("--rclone", type=Path)
    parser.add_argument("--client-public-key", type=Path, help="file holding the one ssh-ed25519 public key allowed to connect")
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help="hard runtime limit, %d to %d (default %d: a lease planned at about 90 minutes plus margin)" % (MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS))
    parser.add_argument("--limit-mib", type=int, default=DEFAULT_LIMIT_MIB, help="stop the endpoint when repository plus log exceed this size (default %d)" % DEFAULT_LIMIT_MIB)
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES, help="stop the endpoint when the repository and log hold more files and directories than this (default %d)" % DEFAULT_MAX_ENTRIES)
    parser.add_argument("--unit", help="(internal, `watch`) the server unit to stop")
    parser.add_argument("--limit-bytes", type=int, help="(internal, `watch`)")
    parser.add_argument("--interval", type=int, default=WATCH_INTERVAL, help="(internal, `watch`)")
    parser.add_argument("--port", type=int, help="loopback port on this machine (default: a free one)")
    parser.add_argument("--host-port", type=int, help="loopback port on the lease host for `forward` (default: same as --port)")
    args = parser.parse_args()
    os.umask(0o077)
    install_signal_handlers()
    try:
        directory = lease.private_directory(args.directory)
        if args.action == "prepare" and args.ssh_keygen and args.client_public_key:
            prepare(directory, args.ssh_keygen, args.client_public_key)
            print(json.dumps({"hostKey": host_key_line(directory)}, sort_keys=True))
        elif args.action == "start" and args.rclone:
            receipt = start(directory, args.rclone, args.seconds, args.port, args.limit_mib, args.max_entries)
            print(json.dumps({key: receipt[key] for key in ("unit", "pid", "port", "watcher", "hostKey", "knownHosts", "repository", "expires", "limitBytes", "maxEntries")}, sort_keys=True))
        elif args.action == "status":
            print(json.dumps(status(directory), sort_keys=True))
        elif args.action == "stop":
            stop(directory)
            print("Throwaway SFTP endpoint stopped; unit, processes and listener verified gone.")
        elif args.action == "watch" and args.unit and args.limit_bytes:
            watch(directory, args.unit, args.limit_bytes, args.max_entries, args.interval)
        elif args.action == "units":
            unit, watcher = unit_names(directory)
            print(json.dumps({"unit": unit, "watcher": watcher}, sort_keys=True))
        elif args.action == "forward":
            print(forward_option(directory, args.host_port))
        else:
            raise lease.Rejected("required arguments missing")
    except KeyboardInterrupt:
        print("Throwaway SFTP operation interrupted; what it had started was stopped.", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print("Throwaway SFTP operation rejected. Private output suppressed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
