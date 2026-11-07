"""Arm and verify a Falcon system timer for independent rehearsal cleanup."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

import rehearsal_lease as lease


SYSTEMD = Path("/run/current-system/sw/bin")
TRANSIENT = Path("/run/systemd/transient")
SERVICE_DEADLINE = "420s"
CREATE_RESERVE = 300
CGROUP = Path("/sys/fs/cgroup/system.slice")


def safe_path(path):
    if not path.is_absolute() or ".." in path.parts or not re.fullmatch(r"/[A-Za-z0-9/._-]+", str(path)):
        raise lease.Rejected("unsafe timer path")
    return str(path)


def calendar(expires):
    return datetime.fromtimestamp(expires, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def timer_settings(directory, manifest, credential):
    run = manifest["run"]
    lease.expected_names(run)
    unit = f"terrace-cloud-{run}"
    script = directory / "rehearsal_lease.py"
    python = Path(sys.executable).resolve(strict=True)
    user = pwd.getpwuid(os.getuid()).pw_name
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user) or os.getuid() == 0:
        raise lease.Rejected("timer user rejected")
    argv = [safe_path(python), "-B", safe_path(script), "cleanup", "--directory", safe_path(directory),
            "--credential-file", safe_path(credential)]
    return unit, user, argv


def system(*args, allow_inactive=False):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=dict(os.environ, TZ="UTC", LC_ALL="C"), text=True, timeout=15, check=False)
    if result.returncode and not allow_inactive:
        raise lease.Rejected("systemd command failed")
    return result.stdout.strip()


def read_property(unit, key):
    return system(SYSTEMD / "systemctl", "show", unit, "--property=" + key, "--value")


def read_properties(unit, keys):
    output = system(SYSTEMD / "systemctl", "show", unit, *("--property=" + key for key in keys))
    return dict(line.split("=", 1) for line in output.splitlines())


def check_ready(directory, manifest, credential, *, now=None):
    if now is None:
        now = time.time()
    if manifest["expires"] <= now + CREATE_RESERVE or os.path.lexists(directory / "closed.json"):
        raise lease.Rejected("create window closed")
    unit, user, argv = timer_settings(directory, manifest, credential)
    receipt = json.loads(lease.read_private(directory / "timer-armed.json", 4096))
    if receipt != {"run": manifest["run"], "expires": manifest["expires"],
                   "scriptSha256": hashlib.sha256(lease.read_private(directory / "rehearsal_lease.py", 65536)).hexdigest()}:
        raise lease.Rejected("timer receipt mismatch")
    timer = unit + ".timer"
    service = unit + ".service"
    timer_file = (TRANSIENT / timer).read_text()
    expiry = calendar(manifest["expires"])
    expected_timer = {"ActiveState": "active", "SubState": "waiting", "Unit": service,
                      "AccuracyUSec": "1s", "RemainAfterElapse": "no"}
    expected_service = {"ActiveState": "inactive", "User": user, "KillMode": "control-group",
                        "Type": "exec", "RuntimeMaxUSec": "7min", "TimeoutStopUSec": "10s",
                        "Restart": "on-failure", "RestartUSec": "20s", "StartLimitIntervalUSec": "2h",
                        "StartLimitBurst": "3", "UMask": "0077", "NoNewPrivileges": "yes", "PrivateTmp": "yes",
                        "ProtectSystem": "strict", "ReadWritePaths": safe_path(directory)}
    timer_status = read_properties(timer, (*expected_timer, "NextElapseUSecRealtime"))
    service_status = read_properties(service, (*expected_service, "ExecStart"))
    next_elapse = timer_status.pop("NextElapseUSecRealtime")
    exec_start = service_status.pop("ExecStart")
    command = r"\{ path=" + re.escape(argv[0]) + r" ; argv\[\]=" + re.escape(" ".join(argv))
    command += r" ; ignore_errors=no ; [^{}]* \}"
    if (timer_status != expected_timer or service_status != expected_service
            or not re.fullmatch(r"[A-Z][a-z]{2} " + re.escape(expiry), next_elapse)
            or "OnCalendar=" + expiry not in timer_file.splitlines() or not re.fullmatch(command, exec_start)):
        raise lease.Rejected("cleanup timer is not ready")


def arm(directory, manifest, credential):
    if not time.time() + CREATE_RESERVE < manifest["expires"] <= time.time() + 21600:
        raise lease.Rejected("timer expiration rejected")
    unit, user, argv = timer_settings(directory, manifest, credential)
    proof = directory / "timer-armed.json"
    if (os.path.lexists(proof) or os.path.lexists(directory / "closed.json")
            or any(read_property(unit + suffix, "LoadState") != "not-found" for suffix in (".timer", ".service"))):
        raise lease.Rejected("timer identity already in use")
    script_sha = hashlib.sha256(lease.read_private(directory / "rehearsal_lease.py", 65536)).hexdigest()
    try:
        system("sudo", "-n", SYSTEMD / "systemd-run", "--quiet", "--collect", "--unit=" + unit,
               "--on-calendar=" + calendar(manifest["expires"]), "--timer-property=AccuracySec=1s",
               "--timer-property=RemainAfterElapse=no", "--property=Type=exec", "--property=User=" + user,
               "--property=RuntimeMaxSec=" + SERVICE_DEADLINE, "--property=TimeoutStopSec=10s",
               "--property=Restart=on-failure", "--property=RestartSec=20s", "--property=StartLimitIntervalSec=7200s",
               "--property=StartLimitBurst=3", "--property=KillMode=control-group", "--property=UMask=0077",
               "--property=NoNewPrivileges=yes", "--property=PrivateTmp=yes", "--property=ProtectSystem=strict",
               "--property=ReadWritePaths=" + safe_path(directory), *argv)
        lease.write_private(proof, {"run": manifest["run"], "expires": manifest["expires"], "scriptSha256": script_sha})
        check_ready(directory, manifest, credential)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        system("sudo", "-n", SYSTEMD / "systemctl", "stop", unit + ".timer", unit + ".service", allow_inactive=True)
        raise


def disarm(directory, manifest, credential):
    if not (directory / "closed.json").is_file():
        raise lease.Rejected("lease is still open")
    receipts = sorted(directory.glob("final-absence-*.json"))
    if not receipts:
        raise lease.Rejected("final absence missing")
    receipt = json.loads(lease.read_private(receipts[-1], 65536))
    if (receipt["run"] != manifest["run"] or receipt["phase"] != "final"
            or receipt["baselinePreserved"] is not True or receipt["absenceReads"] != 3):
        raise lease.Rejected("final absence rejected")
    unit, _, _ = timer_settings(directory, manifest, credential)
    names = (unit + ".timer", unit + ".service")
    system("sudo", "-n", SYSTEMD / "systemctl", "stop", *names, allow_inactive=True)
    if any(read_property(name, "ActiveState") != "inactive" for name in names):
        raise lease.Rejected("timer or cleanup service remains active")
    for key in ("MainPID", "ControlPID"):
        if read_property(names[1], key) not in ("", "0"):
            raise lease.Rejected("cleanup service has a process")
    if read_property(names[1], "ControlGroup"):
        raise lease.Rejected("cleanup service has a cgroup")
    cgroup = CGROUP / names[1]
    try:
        events = (cgroup / "cgroup.events").read_text().splitlines()
    except FileNotFoundError:
        try:
            cgroup.stat()
        except FileNotFoundError:
            return
        raise lease.Rejected("cleanup cgroup events missing") from None
    if [line for line in events if line.startswith("populated ")] != ["populated 0"]:
        raise lease.Rejected("cleanup cgroup is populated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("arm", "ready", "disarm"))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        directory = lease.private_directory(args.directory)
        manifest = json.loads(lease.read_private(directory / "lease.json", 4 * 1024 * 1024))
        token = lease.read_private(args.credential_file, 4096).strip()
        if manifest["schema"] != 1 or manifest["credentialSha256"] != hashlib.sha256(token).hexdigest():
            raise lease.Rejected("timer credential mismatch")
        with os.fdopen(os.open(directory / "lock", os.O_WRONLY | os.O_NOFOLLOW), "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            {"arm": arm, "ready": check_ready, "disarm": disarm}[args.action](directory, manifest, args.credential_file)
        print("Cloud rehearsal cleanup timer verified.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired):
        print("Cloud rehearsal cleanup timer check failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
