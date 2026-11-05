#!/usr/bin/env python3
"""Disposable Linux VM deadline/cancellation proof; root is only for owned PID inspection."""
import json
import os
from pathlib import Path
import signal
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


def owned_guests(marker):
    matches = {}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if not (proc / "comm").read_text().startswith("qemu-system"):
                continue
            # Do not print command lines or inspect any credential/state file.
            if marker.encode() in (proc / "cmdline").read_bytes().split(b"\0"):
                start = fingerprint(int(proc.name))
                if start is not None:
                    matches[int(proc.name)] = start
        except (FileNotFoundError, ProcessLookupError):
            pass
    return matches


def stop_exact(marker, records):
    for pid, start in records.items():
        if owned_guests(marker).get(pid) != start:
            continue
        try:
            fd = os.pidfd_open(pid)
            try:
                if owned_guests(marker).get(pid) == start:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        except ProcessLookupError:
            pass


def run_case(output, mode):
    run_id = uuid.uuid4().hex
    marker = "name=opt/terrace-run,string=" + run_id
    observed = {}
    child = None
    log_path = output / (mode + ".log")
    env = {**os.environ, "CHECK_TIMEOUT": "120", "CHECK_BUILD_TIMEOUT": "35",
           "CHECK_STOP_GRACE": "5", "BUILD_LOCK_TIMEOUT": "900"}
    args = [str(HERE / "run-check"), "--impure", "--file", "check-runner-probe.nix", "--argstr", "runId", run_id]
    try:
        if owned_guests(marker):
            raise RuntimeError("Run marker is already in use.")
        # Build the driver/closure without starting a guest, then time the actual VM.
        with log_path.open("w") as log:
            child = subprocess.Popen([*args, "driver"], cwd=HERE, env={**env, "CHECK_TIMEOUT": "900", "CHECK_BUILD_TIMEOUT": "900"},
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            child.wait(timeout=1830)
            if child.returncode != 0:
                raise RuntimeError("Probe driver build failed.")
            child = subprocess.Popen(args, cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 1050
            sent = False
            while child.poll() is None:
                observed.update(owned_guests(marker))
                if mode == "cancel" and observed and "RUNNER_VM_READY" in log_path.read_text() and not sent:
                    child.send_signal(signal.SIGTERM)
                    sent = True
                if time.monotonic() >= deadline:
                    raise RuntimeError("Proof controller deadline expired.")
                time.sleep(0.1)
        if not observed or "RUNNER_VM_READY" not in log_path.read_text():
            raise RuntimeError("The actual VM never reached its ready marker.")
        if mode == "cancel" and child.returncode != 143:
            raise RuntimeError("Cancellation did not propagate.")
        if mode == "timeout" and (child.returncode == 0 or "timed out" not in log_path.read_text().lower()):
            raise RuntimeError("Expected Nix build timeout was not observed.")
        deadline = time.monotonic() + 20
        while (owned_guests(marker) or any(fingerprint(pid) == start for pid, start in observed.items())) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owned_guests(marker) or any(fingerprint(pid) == start for pid, start in observed.items()):
            raise RuntimeError("An owned guest PID survived check completion.")
        return {"mode": mode, "runId": run_id, "clientExit": child.returncode,
                "observedGuestPids": sorted(observed), "survivingOwnedPids": []}
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        # Failure recovery is restricted to this unique argument + kernel start time.
        # If this path is needed, the case above has already failed, never passed.
        remaining = owned_guests(marker)
        stop_exact(marker, {**observed, **remaining})
        deadline = time.monotonic() + 10
        while owned_guests(marker) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owned_guests(marker):
            raise RuntimeError("Owned guest cleanup failed.")
        if remaining:
            raise RuntimeError("Owned guest needed fallback cleanup; acceptance rejected.")


if __name__ == "__main__":
    try:
        if sys.platform != "linux" or os.geteuid() != 0 or len(sys.argv) != 2 or not os.environ.get("BUILD_LOCK"):
            raise ValueError()
        def interrupted(_signum, _frame):
            raise InterruptedError()

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, interrupted)
        os.umask(0o077)
        output = Path(sys.argv[1]).resolve()
        output.mkdir(mode=0o700)
        results = [run_case(output, mode) for mode in ["timeout", "cancel"]]
        (output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
        print("Native check supervision passed: two ready VMs; timeout/cancel; zero surviving owned guest PIDs.")
    except Exception:
        print("Native check supervision failed; inspect the private proof directory. Raw diagnostics suppressed.", file=sys.stderr)
        sys.exit(1)
