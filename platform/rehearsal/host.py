"""Host containment for disposable rehearsal drivers; no provider operations."""
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent


def fingerprint(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def write_json(path, value):
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def owned_guests(run_id):
    marker = ("name=opt/terrace-rehearsal,string=" + run_id).encode()
    result = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if not (process / "comm").read_text().startswith("qemu-system"):
                continue
            if marker in (process / "cmdline").read_bytes().split(b"\0"):
                start = fingerprint(int(process.name))
                if start is not None:
                    result[int(process.name)] = start
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def create_run(parent):
    parent = Path(parent).resolve(strict=True)
    stat = parent.stat()
    if not parent.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError()
    run_id = uuid.uuid4().hex
    run = parent / ("run-" + run_id)
    run.mkdir(mode=0o700)
    for name in ["state", "evidence", "retained", "escrow"]:
        (run / name).mkdir(mode=0o700)
    write_json(run / "receipt.json", {"schema": 1, "scope": "terrace-disposable-synthetic",
                                     "runId": run_id, "complete": False, "computeOff": False})
    return run, run_id


def command(args, log):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=30, check=True)


def unit_state(unit):
    if not re.fullmatch(r"terrace-rehearsal-[a-f0-9]{32}\.service", unit):
        raise ValueError()
    result = subprocess.run(["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState",
                             "--property=MainPID", "--property=ControlGroup"], capture_output=True, text=True, timeout=10)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode != 0 and values.get("LoadState") != "not-found":
        raise RuntimeError()
    if not {"LoadState", "ActiveState", "MainPID", "ControlGroup"} <= values.keys():
        raise RuntimeError()
    expected = "/system.slice/" + unit
    if values["ControlGroup"] not in ["", expected]:
        raise RuntimeError()
    return values


def cgroup_empty(unit):
    directory = Path("/sys/fs/cgroup/system.slice") / unit
    if not directory.exists():
        return True
    for file in directory.rglob("cgroup.procs"):
        try:
            if file.read_text().strip():
                return False
        except FileNotFoundError:
            pass
    return True


def unit_off(unit):
    state = unit_state(unit)
    return state["ActiveState"] in ["inactive", "failed"] and state["MainPID"] == "0" and cgroup_empty(unit)


def stop_unit(unit, log):
    if not unit_off(unit):
        command(["sudo", "-n", "systemctl", "stop", unit], log)
    deadline = time.monotonic() + 10
    while not unit_off(unit) and time.monotonic() < deadline:
        time.sleep(0.1)
    if not unit_off(unit):
        raise RuntimeError()


def remove_probe_runtime(run, run_id):
    # Only the committed empty/synthetic containment probe calls this. The full
    # data rehearsal must first export/verify retained encrypted state itself.
    record = json.loads((run / "runtime.json").read_text())
    directory = Path(record["directory"])
    if record.get("scope") != "terrace-disposable-synthetic" or record.get("runId") != run_id:
        raise ValueError()
    if directory.parent != Path("/tmp") or not directory.name.startswith("tr-" + run_id[:8] + "-") or directory.is_symlink():
        raise ValueError()
    info = directory.lstat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or not stat.S_ISDIR(info.st_mode) or not unit_off("terrace-rehearsal-" + run_id + ".service"):
        raise ValueError()
    fd = os.open(directory / "owner.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError()
        owner = json.loads(os.read(fd, 4097))
    finally:
        os.close(fd)
    if owner != {"scope": "terrace-disposable-synthetic", "runId": run_id}:
        raise ValueError()
    shutil.rmtree(directory)
    if directory.exists():
        raise RuntimeError()


def contained_driver(run, run_id, driver_command, lock, runtime=1800, queue=3600):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id) or run.name != "run-" + run_id:
        raise ValueError()
    if sys.platform != "linux" or os.geteuid() == 0 or not 1 <= runtime <= 7200 or not 1 <= queue <= 3600:
        raise ValueError()
    if not os.path.isabs(lock) or fingerprint(os.getpid()) is None:
        raise ValueError()
    account = pwd.getpwuid(os.getuid())
    unit = "terrace-rehearsal-" + run_id + ".service"
    # Only public process identity, executable paths and bounds, never secrets.
    write_json(run / "control.json", {"controllerPid": os.getpid(), "controllerStart": fingerprint(os.getpid()),
                                     "runId": run_id, "lock": lock, "queueSeconds": queue, "runtimeSeconds": runtime, "command": driver_command})
    observed = {}
    started = time.monotonic()
    log_path = run / "driver.log"
    with log_path.open("xb") as log:
        os.chmod(log_path, 0o600)
        # Fail closed if transient system units/sudo are unavailable. No daemon,
        # linger, shared host service or persistent unit configuration is changed.
        try:
            command(["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit=" + unit,
                     "--uid=" + account.pw_name, "--gid=" + str(os.getgid()), "--service-type=exec",
                     "--property=WorkingDirectory=" + str(run), "--property=UMask=0077",
                     "--property=Slice=system.slice",
                     "--property=RuntimeMaxSec=" + str(runtime + queue), "--property=TimeoutStopSec=15",
                     "--property=KillMode=control-group", "--property=SendSIGKILL=yes", "--property=Restart=no",
                     "--property=NoNewPrivileges=yes", "--property=TasksMax=4096",
                     "--property=StandardOutput=append:" + str(log_path),
                     "--property=StandardError=append:" + str(log_path),
                     sys.executable, "-B", str(HERE / "guardian.py"), str(run)], log)
            deadline = time.monotonic() + runtime + queue + 30
            while True:
                observed.update(owned_guests(run_id))
                if (run / "guardian-result.json").exists():
                    result = json.loads((run / "guardian-result.json").read_text())
                    break
                state = unit_state(unit)
                if state["ActiveState"] in ["inactive", "failed"]:
                    raise RuntimeError()
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                time.sleep(0.1)
            if result["exit"] != 0 or not result["childrenGone"]:
                raise RuntimeError()
            return {"unit": unit, "observedGuestPids": sorted(observed), "driverSeconds": round(time.monotonic() - started, 2)}
        finally:
            # Exact created unit only. RuntimeMaxSec/KillMode remain independent
            # of this controller's finally block or SSH connection.
            # Also closes an uncertain/late launcher result: any guardian which
            # starts after this point must exit before launching a driver.
            (run / "stop-requested").touch(mode=0o600, exist_ok=True)
            stop_unit(unit, log)
            deadline = time.monotonic() + 10
            while owned_guests(run_id) and time.monotonic() < deadline:
                time.sleep(0.1)
            if owned_guests(run_id) or any(fingerprint(pid) == start for pid, start in observed.items()):
                raise RuntimeError()
