#!/usr/bin/env python3
"""Actual disposable QEMU proof for controller loss and independent containment."""
import json
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

from host import HERE, contained_driver, create_run, fingerprint, owned_guests, remove_probe_runtime, stop_unit, unit_off, write_json


@contextmanager
def reservation(lock):
    # One heavy proof window, not four racy reservations between tiny probes.
    # The case's inner build/runtime locks are private to avoid recursion.
    if not os.path.isabs(lock):
        raise ValueError()
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + 600
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def stop_pid(pid, start, signum):
    fd = os.pidfd_open(pid)
    try:
        if fingerprint(pid) != start:
            raise RuntimeError()
        signal.pidfd_send_signal(fd, signum)
    finally:
        os.close(fd)


def inspect_unit(unit):
    result = subprocess.run(["systemctl", "show", unit, "--property=MainPID", "--value"],
                            capture_output=True, text=True, timeout=10)
    return int(result.stdout.strip() or "0") if result.returncode == 0 else 0


def case(parent, mode, lock):
    started = time.monotonic()
    run, run_id = create_run(parent)
    unit = "terrace-rehearsal-" + run_id + ".service"
    child = None
    warm = None
    sentinel = None
    observed = {}
    guardian = 0
    guardian_start = None
    try:
        with (run / "build.log").open("wb") as log:
            env = {**os.environ, "BUILD_LOCK": lock, "BUILD_LOCK_TIMEOUT": "60", "CHECK_STOP_GRACE": "15",
                   "CHECK_TIMEOUT": "900", "CHECK_BUILD_TIMEOUT": "900"}
            with (run / "build-output.txt").open("wb") as output:
                warm = subprocess.Popen([str(HERE.parent / "run-check"), "--impure", "--file", "rehearsal/containment-probe.nix",
                                         "--argstr", "runId", run_id, "driver"], cwd=HERE.parent, env=env,
                                        stdin=subprocess.DEVNULL, stdout=output, stderr=log, start_new_session=True)
                # Queue60 + wall900 + cleanup<=30 is below this wait. On an
                # exception, finally sends TERM and lets run-check clean its own
                # client group; subprocess.run's immediate timeout-KILL cannot.
                warm.wait(timeout=1000)
                if warm.returncode != 0:
                    raise RuntimeError()
        driver = Path((run / "build-output.txt").read_text().strip()) / "bin/nixos-test-driver"
        if not str(driver).startswith("/nix/store/") or not driver.is_file():
            raise RuntimeError()
        # Outside the test-owned unit; it must survive all three containment paths.
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"])
        with (run / "controller.log").open("wb") as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", str(run), run_id,
                                      str(driver), lock], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     env={**os.environ, "REHEARSAL_TEST_LAUNCH_FAILURE": "1" if mode == "launcher-failure" else "0"})
        deadline = time.monotonic() + 100
        while True:
            observed.update(owned_guests(run_id))
            log = run / "driver.log"
            if observed and log.exists() and "REHEARSAL_PROBE_READY" in log.read_text():
                break
            if child.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError()
            time.sleep(0.1)
        guardian = inspect_unit(unit)
        guardian_start = fingerprint(guardian)
        if guardian < 2 or guardian_start is None:
            raise RuntimeError()
        if mode == "controller-loss":
            stop_pid(child.pid, fingerprint(child.pid), signal.SIGKILL)
        elif mode == "guardian-loss":
            stop_pid(guardian, guardian_start, signal.SIGKILL)
        elif mode == "independent-deadline":
            # Freeze the in-unit guardian: only host systemd can enforce this cap.
            stop_pid(guardian, guardian_start, signal.SIGSTOP)
        elif mode == "launcher-failure":
            (run / "trigger-launch-failure").touch(mode=0o600)
        else:
            raise ValueError()
        child.wait(timeout=130)
        deadline = time.monotonic() + 20
        while (owned_guests(run_id) or fingerprint(guardian) == guardian_start or
               any(fingerprint(pid) == start for pid, start in observed.items())) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owned_guests(run_id) or fingerprint(guardian) == guardian_start or any(fingerprint(pid) == start for pid, start in observed.items()):
            raise RuntimeError()
        if sentinel.poll() is not None or child.returncode == 0 or not unit_off(unit):
            raise RuntimeError()
        if mode == "controller-loss":
            result = json.loads((run / "guardian-result.json").read_text())
            if result != {"exit": 143, "childrenGone": True}:
                raise RuntimeError()
        result = {"mode": mode, "runId": run_id, "unit": unit, "controllerExit": child.returncode,
                  "guardianPid": guardian, "observedGuestPids": sorted(observed), "survivingOwnedPids": [],
                  "guardianStart": guardian_start,
                  "observedGuestIdentities": [{"pid": pid, "start": start} for pid, start in sorted(observed.items())],
                  "unrelatedSentinelSurvived": True}
    finally:
        # Failure cleanup can never turn a failed case into acceptance. Only the
        # exact transient unit and child/sentinel processes created above.
        try:
            with (run / "cleanup.log").open("ab") as log:
                stop_unit(unit, log)
        finally:
            for process in [warm, child, sentinel]:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=35 if process is warm else 5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        if owned_guests(run_id) or not unit_off(unit):
            raise RuntimeError()
        if (run / "runtime.json").exists():
            remove_probe_runtime(run, run_id)
    result["caseSeconds"] = round(time.monotonic() - started, 2)
    write_json(run / "receipt.json", {"schema": 1, "scope": "terrace-disposable-synthetic",
                                     "runId": run_id, "complete": True, "computeOff": True, "containment": result})
    return result


if __name__ == "__main__":
    try:
        os.umask(0o077)
        def interrupted(_signum, _frame):
            raise InterruptedError()

        for signum in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
            signal.signal(signum, interrupted)
        if sys.argv[1:2] == ["--child"]:
            _, _, path, run_id, driver, lock = sys.argv
            if any(os.path.realpath("/proc/self/fd/" + name) == os.environ["BUILD_LOCK"] for name in os.listdir("/proc/self/fd")):
                raise RuntimeError("The controller inherited the harness reservation.")
            if os.environ.get("REHEARSAL_TEST_LAUNCH_FAILURE") == "1":
                import host
                original = host.command

                def uncertain_launch(args, log):
                    original(args, log)
                    deadline = time.monotonic() + 80
                    while not (Path(path) / "trigger-launch-failure").exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        time.sleep(0.05)
                    raise RuntimeError("Synthetic launcher failed after unit creation.")

                host.command = uncertain_launch
            contained_driver(Path(path), run_id, [driver, "--no-interactive", "--output_directory", str(Path(path) / "evidence")], lock, runtime=35, queue=60)
            raise RuntimeError("The deliberately hanging probe must not succeed.")
        if len(sys.argv) != 2 or sys.platform != "linux" or os.geteuid() == 0:
            raise ValueError()
        lock = os.environ["BUILD_LOCK"]
        with reservation(lock):
            private_lock = str(Path(sys.argv[1]).resolve() / "case.lock")
            results = [case(sys.argv[1], mode, private_lock) for mode in ["controller-loss", "guardian-loss", "independent-deadline", "launcher-failure"]]
        write_json(Path(sys.argv[1]) / "containment-result.json", results)
        print("Independent rehearsal containment passed: four ready guests, controller/guardian loss, host deadline, launcher failure, no owned survivors; unrelated sentinels preserved.")
    except BaseException:
        print("Independent rehearsal containment failed; inspect protected proof files. No raw diagnostics emitted.", file=sys.stderr)
        sys.exit(1)
