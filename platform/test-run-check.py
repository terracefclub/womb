#!/usr/bin/env python3
"""Synthetic process/lock regression; no provider, real data or VM is touched."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

RUNNER = Path(__file__).with_name("run-check")


def alive(pid):
    try:
        # A terminated zombie consumes no compute, but still reap owned leaders.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@unittest.skipUnless(sys.platform == "linux", "Linux process/lock semantics")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="terrace-runner-test-")
        self.root = Path(self.temporary.name)
        self.lock = self.root / "lock"
        self.record = self.root / "pids.json"
        self.env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                    "BUILD_LOCK": str(self.lock), "CHECK_TIMEOUT": "3", "CHECK_BUILD_TIMEOUT": "2",
                    "CHECK_STOP_GRACE": "1", "BUILD_LOCK_TIMEOUT": "2", "RECORD": str(self.record)}
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        # A broken supervisor must not leave its synthetic fixtures running.
        try:
            records = json.loads(self.record.read_text())
            for record in records:
                if not isinstance(record, dict):
                    continue
                pid = record["pid"]
                try:
                    fd = os.pidfd_open(pid)
                    try:
                        current = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[19]
                        if current == record["start"]:
                            signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                except (FileNotFoundError, ProcessLookupError):
                    pass
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        self.temporary.cleanup()

    def fake(self, body):
        path = self.root / "nix"
        path.write_text(f"#!{sys.executable}\nimport os,json,signal,subprocess,sys,time\nfrom pathlib import Path\n"
                        "def record(pids):\n records=[{'pid':p,'start':Path(f'/proc/{p}/stat').read_text().split(') ',1)[1].split()[19]} for p in pids]\n open(os.environ['RECORD'],'w').write(json.dumps(records))\n" + body)
        path.chmod(0o700)

    def start(self, **changes):
        child = subprocess.Popen([str(RUNNER), "synthetic-check"], env={**self.env, **changes},
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        return child

    def result(self, child):
        out, err = child.communicate(timeout=10)
        return child.returncode, out, err

    def wait_record(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return json.loads(self.record.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.02)
        self.fail("synthetic child did not start")

    def unlocked(self):
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_exit_status_and_nix_build_deadline(self):
        self.fake("open(os.environ['RECORD'],'w').write(json.dumps(sys.argv[1:]));sys.exit(7)\n")
        code, _, _ = self.result(self.start())
        self.assertEqual(code, 7)
        args = json.loads(self.record.read_text())
        self.assertEqual(args[0:2], ["build", "synthetic-check"])
        self.assertEqual(args[args.index("--timeout") + 1], "2")
        self.unlocked()

    def test_invalid_limits_do_not_start_a_child_or_echo_input(self):
        self.fake("open(os.environ['RECORD'],'w').write('started')\n")
        for key, value in [("CHECK_TIMEOUT", "0"), ("CHECK_TIMEOUT", "14401"),
                           ("CHECK_TIMEOUT", "PRIVATE_LIMIT_MARKER"), ("CHECK_BUILD_TIMEOUT", "4"),
                           ("CHECK_STOP_GRACE", "0"), ("BUILD_LOCK_TIMEOUT", "0")]:
            with self.subTest(key=key, shape=len(value)):
                code, out, err = self.result(self.start(**{key: value}))
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn(value, err)
                self.assertFalse(self.record.exists())

    def test_lock_timeout_never_launches_a_build(self):
        self.fake("open(os.environ['RECORD'],'w').write('started')\n")
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            code, _, _ = self.result(self.start(BUILD_LOCK_TIMEOUT="1"))
        self.assertEqual(code, 75)
        self.assertFalse(self.record.exists())

    def test_timeout_rejects_graceful_exit_zero(self):
        self.fake("signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))\nrecord([os.getpid()])\ntime.sleep(60)\n")
        child = self.start(CHECK_TIMEOUT="1", CHECK_BUILD_TIMEOUT="1")
        pids = self.wait_record()
        self.assertEqual(self.result(child)[0], 124)
        self.assertTrue(all(not alive(record["pid"]) for record in pids))
        self.unlocked()

    def test_cancellation_escalates_only_owned_group(self):
        sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        self.children.append(sentinel)
        self.fake("signal.signal(signal.SIGTERM,signal.SIG_IGN)\nchild=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(\"READY\",flush=True);time.sleep(60)'],stdout=subprocess.PIPE)\nchild.stdout.readline()\nrecord([os.getpid(),child.pid])\ntime.sleep(60)\n")
        child = self.start(CHECK_TIMEOUT="10")
        pids = self.wait_record()
        child.send_signal(signal.SIGTERM)
        self.assertEqual(self.result(child)[0], 143)
        self.assertTrue(all(not alive(record["pid"]) for record in pids))
        self.assertIsNone(sentinel.poll())
        self.unlocked()

    def test_success_still_stops_stray_owned_child(self):
        self.fake("child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(\"READY\",flush=True);time.sleep(60)'],stdout=subprocess.PIPE)\nchild.stdout.readline()\nrecord([child.pid])\nsys.exit(0)\n")
        child = self.start()
        records = self.wait_record()
        self.assertEqual(self.result(child)[0], 0)
        self.assertTrue(all(not alive(record["pid"]) for record in records))
        self.unlocked()

    def test_build_cannot_inherit_lock_descriptor(self):
        self.fake("fds=[]\nfor name in os.listdir('/proc/self/fd'):\n try:\n  if os.path.realpath('/proc/self/fd/'+name)==os.environ['BUILD_LOCK']:fds.append(name)\n except OSError:pass\nopen(os.environ['RECORD'],'w').write(json.dumps(fds))\n")
        self.assertEqual(self.result(self.start())[0], 0)
        self.assertEqual(json.loads(self.record.read_text()), [])
        self.unlocked()


if __name__ == "__main__":
    unittest.main()
