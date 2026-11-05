#!/usr/bin/env python3
"""Transient-unit main process: own the lock and stop if its controller dies."""
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

from host import write_json


def fingerprint(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def cgroup_members():
    """Only our systemd service cgroup; never select by command/name/cwd."""
    group = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    if not group.startswith("/system.slice/terrace-rehearsal-") or not group.endswith(".service"):
        raise RuntimeError()
    directory = Path("/sys/fs/cgroup") / group.lstrip("/")
    records = {}
    for file in directory.rglob("cgroup.procs"):
        for value in file.read_text().split():
            pid = int(value)
            start = fingerprint(pid)
            if pid != os.getpid() and start is not None:
                records[pid] = start
    return records


def signal_members(records, signum):
    for pid, start in records.items():
        try:
            fd = os.pidfd_open(pid)
            try:
                if fingerprint(pid) == start:
                    signal.pidfd_send_signal(fd, signum)
            finally:
                os.close(fd)
        except ProcessLookupError:
            pass


def main():
    # The caller creates this owner-only directory and supplies public PID/start
    # identity. No password, token or application input is accepted on argv.
    run = Path(sys.argv[1]).resolve(strict=True)
    config = json.loads((run / "control.json").read_text())
    controller = int(config["controllerPid"])
    if fingerprint(controller) != config["controllerStart"]:
        raise RuntimeError()
    controller_fd = os.pidfd_open(controller)
    if fingerprint(controller) != config["controllerStart"]:
        os.close(controller_fd)
        raise RuntimeError()
    lock_fd = os.open(config["lock"], os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    child = None
    stopped = False

    def cancel(_signum, _frame):
        nonlocal stopped
        stopped = True

    for signum in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(signum, cancel)
    try:
        deadline = time.monotonic() + config["queueSeconds"]
        while True:
            if stopped or (run / "stop-requested").exists() or select.select([controller_fd], [], [], 0)[0]:
                return 143
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return 75
                time.sleep(0.1)
        # A new process group is useful, while the independent systemd cgroup
        # remains the containment boundary even if a descendant calls setsid.
        # AF_UNIX has a108-byte path bound. Deep owner output paths cannot hold
        # the driver's VDE/vsock sockets. Keep runtime short, private and recorded;
        # callers decide when disposable plaintext state may be removed.
        runtime = Path(tempfile.mkdtemp(prefix="tr-" + config["runId"][:8] + "-", dir="/tmp"))
        write_json(runtime / "owner.json", {"scope": "terrace-disposable-synthetic", "runId": config["runId"]})
        write_json(run / "runtime.json", {"scope": "terrace-disposable-synthetic", "runId": config["runId"], "directory": str(runtime)})
        environment = {**os.environ, "TMPDIR": str(runtime), "XDG_RUNTIME_DIR": str(runtime),
                       "TERRACE_REHEARSAL_RUN_DIR": str(run), "PYTHONUNBUFFERED": "1"}
        child = subprocess.Popen(config["command"], cwd=runtime, env=environment,
                                 stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + config["runtimeSeconds"]
        while child.poll() is None:
            if stopped or (run / "stop-requested").exists() or select.select([controller_fd], [], [], 0.1)[0]:
                return 143
            if time.monotonic() >= deadline:
                return 124
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        try:
            remaining = cgroup_members()
            signal_members(remaining, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while cgroup_members() and time.monotonic() < deadline:
                if child is not None:
                    child.poll()
                time.sleep(0.05)
            signal_members(cgroup_members(), signal.SIGKILL)
            if child is not None:
                child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while cgroup_members() and time.monotonic() < deadline:
                time.sleep(0.05)
            if cgroup_members():
                raise RuntimeError()
        finally:
            os.close(lock_fd)
            os.close(controller_fd)


if __name__ == "__main__":
    try:
        result = main()
        # Written only after the finally cleanup has proved our cgroup empty.
        path = Path(sys.argv[1]) / "guardian-result.json"
        write_json(path, {"exit": result, "childrenGone": True})
        sys.exit(result)
    except Exception:
        # Do not echo input paths, commands, environments or provider diagnostics.
        print("Rehearsal guardian failed; inspect the protected run evidence.", file=sys.stderr)
        sys.exit(2)
