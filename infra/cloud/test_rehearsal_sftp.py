import io
import json
import os
from pathlib import Path
import re
import tempfile
import signal
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch

import rehearsal_lease as lease
import rehearsal_sftp as sftp


CLIENT = "ssh-ed25519 " + "A" * 68
HOST = "ssh-ed25519 " + "B" * 68
RCLONE = Path("/nix/store/" + "a" * 32 + "-rclone-1.75.2/bin/rclone")
KEYGEN = Path("/nix/store/" + "b" * 32 + "-openssh-10.6p1/bin/ssh-keygen")


def run_directory():
    holder = tempfile.TemporaryDirectory(prefix="sftp-")
    os.chmod(holder.name, 0o700)
    return holder, Path(holder.name)


def fake_keygen(argv, **_):
    target = Path(argv[argv.index("-f") + 1])
    target.write_text("synthetic private key material\n")
    Path(str(target) + ".pub").write_text(HOST + " terrace-lease-sftp-host\n")


class PublicKeyTest(unittest.TestCase):
    def read(self, text):
        holder, root = run_directory()
        with holder:
            (root / "k.pub").write_text(text)
            return sftp.read_public_key(root / "k.pub")

    def test_one_ed25519_public_key_only(self):
        self.assertEqual(self.read(CLIENT + " probe\n"), CLIENT)
        for bad in ("", CLIENT + "\n" + CLIENT + "\n", "ssh-rsa " + "A" * 372 + "\n",
                    "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "A" * 68 + "\n-----END OPENSSH PRIVATE KEY-----\n",
                    'command="sh" ' + CLIENT + "\n", CLIENT[:-1] + "\n"):
            with self.assertRaises(lease.Rejected, msg=bad):
                self.read(bad)


class PrepareTest(unittest.TestCase):
    def prepared(self):
        holder, root = run_directory()
        key = root / "client.pub"
        key.write_text(CLIENT + " c\n")
        with patch.object(sftp.subprocess, "run", side_effect=fake_keygen), patch.object(Path, "is_file", return_value=True):
            sftp.prepare(root, KEYGEN, key)
        return holder, root

    def test_creates_exactly_the_run_material_with_private_modes(self):
        holder, root = self.prepared()
        with holder:
            self.assertEqual(sorted(path.name for path in root.iterdir()), sorted(["client.pub", sftp.HOST_KEY, sftp.HOST_PUB, sftp.AUTHORIZED, sftp.REPOSITORY, sftp.LOGS]))
            self.assertEqual((root / sftp.AUTHORIZED).read_text(), CLIENT + "\n")
            self.assertEqual(sftp.host_key_line(root), HOST)
            self.assertEqual(list((root / sftp.REPOSITORY).iterdir()), [])
            for name in (sftp.HOST_KEY, sftp.HOST_PUB, sftp.AUTHORIZED):
                self.assertEqual(os.stat(root / name).st_mode & 0o777, 0o600)

    def test_refuses_any_existing_file_and_unpinned_tools(self):
        holder, root = run_directory()
        with holder:
            key = root / "client.pub"
            key.write_text(CLIENT + "\n")
            for name in (sftp.HOST_KEY, sftp.HOST_PUB, sftp.AUTHORIZED, sftp.REPOSITORY, sftp.LOGS, sftp.RECEIPT):
                path = root / name
                path.touch()
                with patch.object(sftp.subprocess, "run", side_effect=fake_keygen), patch.object(Path, "is_file", return_value=True):
                    with self.assertRaises(lease.Rejected):
                        sftp.prepare(root, KEYGEN, key)
                path.unlink()
            with self.assertRaises(lease.Rejected):
                sftp.prepare(root, Path("/usr/bin/ssh-keygen"), key)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["client.pub"])

    def test_second_prepare_changes_nothing(self):
        holder, root = self.prepared()
        with holder:
            before = (root / sftp.HOST_KEY).read_bytes()
            with patch.object(sftp.subprocess, "run", side_effect=fake_keygen), patch.object(Path, "is_file", return_value=True):
                with self.assertRaises(lease.Rejected):
                    sftp.prepare(root, KEYGEN, root / "client.pub")
            self.assertEqual((root / sftp.HOST_KEY).read_bytes(), before)

    def test_cli_never_prints_private_material(self):
        holder, root = run_directory()
        with holder:
            key = root / "client.pub"
            key.write_text(CLIENT + "\n")
            out = io.StringIO()
            with (patch.object(sftp.subprocess, "run", side_effect=fake_keygen), patch.object(Path, "is_file", return_value=True),
                  patch("sys.stdout", out), patch("sys.argv", ["x", "prepare", "--directory", str(root), "--ssh-keygen", str(KEYGEN),
                                                               "--client-public-key", str(key)])):
                self.assertEqual(sftp.main(), 0)
            self.assertNotIn("private", out.getvalue())
            self.assertIn(HOST, out.getvalue())


class ServerTest(unittest.TestCase):
    def test_exact_server_command(self):
        holder, root = run_directory()
        with holder:
            run = str(root.resolve())
            self.assertEqual(sftp.server_command(root, RCLONE, 40222), [
                str(RCLONE), "serve", "sftp", ":local:/var/empty", "--addr", "127.0.0.1:40222",
                "--key", run + "/sftp-host-key", "--authorized-keys", run + "/sftp-authorized-keys", "--config", "/dev/null",
                "--log-file", run + "/sftp-logs/rclone.log", "--log-level", "ERROR", "--log-file-max-size", "5M",
                "--log-file-max-backups", "2", "--vfs-cache-mode", "off", "--no-modtime", "--umask", "077"])

    def test_exact_server_properties(self):
        holder, root = run_directory()
        with holder:
            run = str(root.resolve())
            self.assertEqual(sftp.server_properties(root, 600, "w.service"), [
                "--property=Type=exec", "--property=RuntimeMaxSec=600", "--property=TimeoutStopSec=10s",
                "--property=KillMode=control-group", "--property=UMask=0077", "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes", "--property=PrivateDevices=yes", "--property=ProtectSystem=strict",
                "--property=ProtectHome=tmpfs", "--property=ProtectKernelTunables=yes", "--property=ProtectControlGroups=yes",
                "--property=RestrictAddressFamilies=AF_INET AF_UNIX", "--property=MemoryMax=1G", "--property=TasksMax=256",
                "--property=LimitNOFILE=4096", "--property=BindsTo=w.service", "--property=After=w.service",
                "--property=TemporaryFileSystem=" + run + ":ro /var/empty:ro",
                "--property=BindReadOnlyPaths=" + run + "/sftp-host-key " + run + "/sftp-authorized-keys",
                "--property=BindPaths=" + run + "/sftp-logs " + run + "/app:/var/empty/app",
                "--property=WorkingDirectory=/var/empty"])

    def test_exact_watcher_unit(self):
        holder, root = run_directory()
        with holder:
            run = str(root.resolve())
            self.assertEqual(sftp.watcher_properties(root, 600), [
                "--property=Type=exec", "--property=RuntimeMaxSec=600", "--property=TimeoutStopSec=10s",
                "--property=KillMode=control-group", "--property=UMask=0077", "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes", "--property=PrivateDevices=yes", "--property=ProtectSystem=strict",
                "--property=ProtectHome=read-only", "--property=ReadWritePaths=" + run,
                "--property=RestrictAddressFamilies=AF_UNIX", "--property=MemoryMax=256M", "--property=TasksMax=32"])
            command = sftp.watcher_command(root, "terrace-lease-sftp-" + "1" * 16 + ".service", 1048576, 5000)
            self.assertEqual(command[1:3], ["-B", str(Path(sftp.__file__).resolve())])
            self.assertEqual(command[3:], ["watch", "--directory", run, "--unit", "terrace-lease-sftp-" + "1" * 16 + ".service",
                                           "--limit-bytes", "1048576", "--max-entries", "5000", "--interval", "2"])

    def test_unsafe_paths_rejected(self):
        for bad in ("relative", "/a/../b", "/a b", "/a;b", "/a$b"):
            with self.assertRaises(lease.Rejected):
                sftp.safe_path(bad)

    def test_start_preflight(self):
        holder, root = run_directory()
        with holder:
            with patch.object(Path, "is_file", return_value=True):
                for seconds in (0, 299, 14401, 21600):
                    with self.assertRaises(lease.Rejected):
                        sftp.start(root, RCLONE, seconds)
                for limit in (0, 15, 65537):
                    with self.assertRaises(lease.Rejected):
                        sftp.start(root, RCLONE, 600, limit_mib=limit)
                for entries in (0, 999, 5000001):
                    with self.assertRaises(lease.Rejected):
                        sftp.start(root, RCLONE, 600, max_entries=entries)
                with self.assertRaises(lease.Rejected):
                    sftp.start(root, Path("/usr/bin/rclone"), 600)
                for name in (sftp.RECEIPT, sftp.STOPPED, sftp.TRIPPED):
                    (root / name).touch()
                    with self.assertRaises(lease.Rejected):
                        sftp.start(root, RCLONE, 600)
                    (root / name).unlink()

    def test_free_port_is_unprivileged_loopback(self):
        self.assertGreaterEqual(sftp.free_port(), 1024)

    def test_default_runtime_fits_the_lease(self):
        self.assertEqual((sftp.DEFAULT_SECONDS, sftp.MAX_SECONDS), (7200, 14400))


class NamespaceTest(unittest.TestCase):
    def tree(self, served=("app",), visible=(sftp.HOST_KEY, sftp.AUTHORIZED, sftp.LOGS)):
        holder, root = run_directory()
        proc = root / "proc"
        base = proc / "77" / "root"
        (base / "var/empty").mkdir(parents=True)
        for name in served:
            (base / "var/empty" / name).mkdir()
        run = str(root.resolve()).lstrip("/")
        (base / run).mkdir(parents=True)
        for name in visible:
            (base / run / name).touch()
        return holder, root, proc

    def test_exact_layout_passes_and_anything_else_fails(self):
        for served, visible, ok in (((), None, False), (("app",), None, True), (("app", "x"), None, False), (("other",), None, False),
                                    (("app",), (sftp.HOST_KEY, sftp.AUTHORIZED), False),
                                    (("app",), (sftp.HOST_KEY, sftp.AUTHORIZED, sftp.LOGS, sftp.RECEIPT), False)):
            holder, root, proc = self.tree(served, visible if visible is not None else (sftp.HOST_KEY, sftp.AUTHORIZED, sftp.LOGS))
            with holder:
                if ok:
                    sftp.check_namespace(77, root, proc)
                else:
                    with self.assertRaises(lease.Rejected):
                        sftp.check_namespace(77, root, proc)

    def test_unreadable_process_fails(self):
        holder, root, proc = self.tree()
        with holder:
            with self.assertRaises(lease.Rejected):
                sftp.check_namespace(78, root, proc)


def bounded(action=lambda: None, limit=10):
    """A sleep that fails the test instead of letting a broken watcher loop forever."""
    calls = []

    def sleeper(_):
        calls.append(1)
        if len(calls) > limit:
            raise AssertionError("watcher kept looping")
        action()
    return sleeper


class WatchTest(unittest.TestCase):
    UNIT = "terrace-lease-sftp-" + "2" * 16 + ".service"

    def run_dir(self):
        holder, root = run_directory()
        (root / sftp.REPOSITORY).mkdir()
        (root / sftp.LOGS).mkdir()
        return holder, root

    def test_stops_the_server_over_the_limit_and_records_it(self):
        holder, root = self.run_dir()
        with holder:
            (root / sftp.REPOSITORY / "pack").write_bytes(b"x" * 100)
            grow = lambda: (root / sftp.REPOSITORY / "pack").write_bytes(b"x" * 50000)
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 10000, 100, 2, sleep=bounded(grow)))
                self.assertEqual(systemctl.call_args.args[:2], ("stop", self.UNIT))
            marker = json.loads((root / sftp.TRIPPED).read_text())
            self.assertEqual((marker["limitBytes"], marker["maxEntries"]), (10000, 100))

    def test_counts_the_log_and_ends_with_the_server(self):
        holder, root = self.run_dir()
        with holder:
            (root / sftp.LOGS / "rclone.log").write_bytes(b"x" * 20000)
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 10000, 100, 2, sleep=bounded()))
                self.assertTrue(systemctl.called)
            (root / sftp.TRIPPED).unlink()
            states = iter([{"ActiveState": "active"}, {"ActiveState": "inactive"}])
            (root / sftp.LOGS / "rclone.log").write_bytes(b"x")
            with patch.object(sftp, "properties", side_effect=lambda *a: next(states)), patch.object(sftp, "systemctl") as systemctl:
                self.assertFalse(sftp.watch(root, self.UNIT, 10000, 100, 2, sleep=bounded()))
                self.assertFalse(systemctl.called)

    def test_empty_files_and_directories_count(self):
        holder, root = self.run_dir()
        with holder:
            self.assertEqual(sftp.usage(root), (0, 0))
            (root / sftp.REPOSITORY / "d").mkdir()
            for index in range(3):
                (root / sftp.REPOSITORY / "d" / str(index)).touch()
            (root / sftp.LOGS / "log").touch()
            self.assertEqual(sftp.usage(root), (5 * sftp.ENTRY_BYTES, 5))
            # Three empty files and a directory exceed a 4 * block limit by bytes alone.
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 4 * sftp.ENTRY_BYTES, 1000, 2, sleep=bounded()))
                self.assertTrue(systemctl.called)

    def test_entry_cap_trips_before_the_byte_limit(self):
        holder, root = self.run_dir()
        with holder:
            for index in range(6):
                (root / sftp.REPOSITORY / str(index)).touch()
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 10**12, 5, 2, sleep=bounded()))
                self.assertTrue(systemctl.called)
            marker = json.loads((root / sftp.TRIPPED).read_text())
            self.assertEqual((marker["entries"], marker["maxEntries"]), (6, 5))
            (root / sftp.TRIPPED).unlink()
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 10**12, 5, 2, sleep=bounded()) is True)
            (root / sftp.TRIPPED).unlink()
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertRaises(AssertionError, sftp.watch, root, self.UNIT, 10**12, 6, 2, bounded())
                self.assertFalse(systemctl.called)

    def test_gives_up_when_the_server_never_appears_and_refuses_foreign_units(self):
        holder, root = self.run_dir()
        with holder:
            with patch.object(sftp, "properties", return_value={"ActiveState": "inactive"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertFalse(sftp.watch(root, self.UNIT, 1000, 100, 30, sleep=bounded()))
                self.assertFalse(systemctl.called)
                for unit in ("sshd.service", self.UNIT[:-8] + "-watch.service"):
                    with self.assertRaises(lease.Rejected):
                        sftp.watch(root, unit, 1000, 100, 2)


class StartTest(unittest.TestCase):
    """start() with systemd-run, systemd state, the listener and the sandbox check replaced."""

    def prepared(self):
        holder, root = run_directory()
        key = root / "client.pub"
        key.write_text(CLIENT + "\n")
        with patch.object(sftp.subprocess, "run", side_effect=fake_keygen), patch.object(Path, "is_file", return_value=True):
            sftp.prepare(root, KEYGEN, key)
        return holder, root

    def run_start(self, root, watcher_state="active", layout=None, after_stop="inactive"):
        calls, state = [], {"stopped": False}

        def run(argv, **_):
            calls.append([part for part in argv if part.startswith("--unit=")][0][len("--unit="):])
            return type("Done", (), {"returncode": 0})()

        def properties(name, keys):
            if keys == ["LoadState"]:
                return {"LoadState": "not-found"}
            if state["stopped"]:
                return {"ActiveState": after_stop}
            if name.endswith("-watch.service"):
                return {"ActiveState": watcher_state}
            return {"ActiveState": "active", "MainPID": "4242"}

        def systemctl(*args, **_):
            state["stopped"] = args[0] == "stop"
        banners = iter([None] + [True] * 10)
        patches = (patch.object(sftp.subprocess, "run", side_effect=run), patch.object(sftp, "properties", side_effect=properties),
                   patch.object(sftp, "systemctl", side_effect=systemctl), patch.object(sftp, "banner", side_effect=lambda *a: next(banners)),
                   patch.object(sftp, "free_port", return_value=40222), patch.object(sftp.time, "sleep"), patch.object(Path, "is_file", return_value=True),
                   patch.object(sftp, "check_namespace", side_effect=layout) if layout else patch.object(sftp, "check_namespace"))
        for item in patches:
            item.start()
        self.addCleanup(patch.stopall)
        return calls

    def test_launches_the_watcher_first_and_writes_the_receipt(self):
        holder, root = self.prepared()
        with holder:
            calls = self.run_start(root)
            receipt = sftp.start(root, RCLONE, 600, limit_mib=32, max_entries=2000)
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0].endswith("-watch.service") and calls[1] == receipt["unit"])
            self.assertEqual((receipt["limitBytes"], receipt["maxEntries"], receipt["pid"]), (32 * 1024 * 1024, 2000, 4242))
            self.assertEqual(sftp.read_receipt(root)["watcher"], calls[0])

    def test_failing_layout_check_stops_both_units_and_writes_no_receipt(self):
        holder, root = self.prepared()
        with holder:
            self.run_start(root, layout=lease.Rejected("sandbox layout check failed"))
            with self.assertRaises(lease.Rejected):
                sftp.start(root, RCLONE, 600)
            sftp.systemctl.assert_called_with("stop", sftp.systemctl.call_args.args[1], sftp.systemctl.call_args.args[2], allow_inactive=True)
            self.assertTrue(sftp.systemctl.call_args.args[2].endswith("-watch.service"))
            self.assertFalse((root / sftp.RECEIPT).exists())

    def test_inactive_watcher_aborts_start(self):
        holder, root = self.prepared()
        with holder:
            self.run_start(root, watcher_state="inactive")
            with self.assertRaises(lease.Rejected):
                sftp.start(root, RCLONE, 600)
            self.assertEqual(sftp.systemctl.call_args.args[0], "stop")
            self.assertFalse(sftp.check_namespace.called)
            self.assertFalse((root / sftp.RECEIPT).exists())

    def test_a_unit_that_survives_the_stop_is_reported(self):
        holder, root = self.prepared()
        with holder:
            self.run_start(root, layout=lease.Rejected("sandbox layout check failed"), after_stop="active")
            with self.assertRaises(lease.Rejected) as caught:
                sftp.start(root, RCLONE, 600)
            self.assertIn("still running", str(caught.exception))
            self.assertFalse((root / sftp.RECEIPT).exists())


class SignalTest(unittest.TestCase):
    """An interrupted `start` must stop what it launched: TERM and HUP behave like INT (a real signal)."""

    def test_term_and_hup_reach_the_clean_up(self):
        child = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(sftp.__file__).parent)!r})
            import rehearsal_sftp as s
            s.install_signal_handlers()
            print("ready", flush=True)
            try:
                time.sleep(30)
            except BaseException:
                print("clean-up ran", flush=True)
                sys.exit(7)
        """)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                process = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.send_signal(sig)
                out, _ = process.communicate(timeout=20)
                self.assertEqual((process.returncode, out.strip()), (7, "clean-up ran"))

    def test_an_interrupted_start_stops_both_units(self):
        holder, root = StartTest.prepared(self)
        with holder:
            StartTest.run_start(self, root)

            def interrupted(*_):
                raise KeyboardInterrupt
            with patch.object(sftp, "banner", side_effect=[None, KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):
                    sftp.start(root, RCLONE, 600)
            unit, watcher = sftp.unit_names(root)
            sftp.systemctl.assert_called_with("stop", unit, watcher, allow_inactive=True)
            self.assertFalse((root / sftp.RECEIPT).exists())


class UnitNameTest(unittest.TestCase):
    def test_names_belong_to_one_run_directory(self):
        holder_a, a = run_directory()
        holder_b, b = run_directory()
        with holder_a, holder_b:
            ua, wa = sftp.unit_names(a)
            ub, wb = sftp.unit_names(b)
            self.assertEqual(sftp.unit_names(a), (ua, wa))
            self.assertNotEqual(ua, ub)
            self.assertTrue(sftp.UNIT.fullmatch(ua) and sftp.UNIT.fullmatch(wa) and wa == ua[:-8] + "-watch.service")


class UnitNameBasenameTest(unittest.TestCase):
    def test_two_directories_with_the_same_basename_get_different_names(self):
        holder, root = run_directory()
        with holder:
            a, b = root / "one" / "sftp", root / "two" / "sftp"
            a.mkdir(parents=True)
            b.mkdir(parents=True)
            self.assertNotEqual(sftp.unit_names(a), sftp.unit_names(b))
            link = root / "link"
            link.symlink_to(a)
            self.assertEqual(sftp.unit_names(link), sftp.unit_names(a), "the realpath names the run")


class InterruptedExitTest(unittest.TestCase):
    def test_an_interrupted_helper_exits_130(self):
        holder, root = run_directory()
        with holder, patch.object(sys, "argv", ["rehearsal_sftp.py", "start", "--directory", str(root), "--rclone", str(RCLONE)]), \
                patch.object(sftp, "start", side_effect=KeyboardInterrupt), patch.object(sftp, "install_signal_handlers"), \
                patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(sftp.main(), 130)
        self.assertIn("interrupted", err.getvalue())


class StopTest(unittest.TestCase):
    def receipt(self, root, pid=0):
        lease.write_private(root / sftp.RECEIPT, {"schema": 1, "unit": "terrace-lease-sftp-" + "0" * 16 + ".service",
                                                  "watcher": "terrace-lease-sftp-" + "0" * 16 + "-watch.service", "pid": pid,
                                                  "address": "127.0.0.1", "port": 40222})

    def test_stop_requires_inactive_units_and_silent_listener(self):
        holder, root = run_directory()
        with holder:
            self.receipt(root)
            gone = {"ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""}
            with patch.object(sftp, "systemctl") as systemctl, patch.object(sftp, "properties", return_value=gone), patch.object(sftp, "banner", return_value=None):
                sftp.stop(root)
                self.assertEqual(systemctl.call_args.args[:3], ("stop", "terrace-lease-sftp-" + "0" * 16 + ".service",
                                                                "terrace-lease-sftp-" + "0" * 16 + "-watch.service"))
                self.assertTrue((root / sftp.STOPPED).exists())
                sftp.stop(root)  # idempotent
            (root / sftp.STOPPED).unlink()
            for facts, answer in (({**gone, "ActiveState": "active"}, None), ({**gone, "MainPID": "42"}, None),
                                  ({**gone, "ControlGroup": "/x"}, None), (gone, True)):
                with patch.object(sftp, "systemctl"), patch.object(sftp, "properties", return_value=facts), patch.object(sftp, "banner", return_value=answer):
                    with self.assertRaises(lease.Rejected):
                        sftp.stop(root)
            self.assertFalse((root / sftp.STOPPED).exists())

    def test_stop_fails_while_the_server_process_is_still_there(self):
        holder, root = run_directory()
        with holder:
            self.receipt(root, pid=91)
            gone = {"ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""}
            proc = root / "proc"
            (proc / "91").mkdir(parents=True)
            (proc / "91" / "cmdline").write_bytes(b"/nix/store/x-rclone-1.75.2/bin/rclone\x00serve\x00sftp\x00--addr\x00127.0.0.1:40222\x00")
            with (patch.object(sftp, "PROC", proc), patch.object(sftp, "systemctl"), patch.object(sftp, "properties", return_value=gone),
                  patch.object(sftp, "banner", return_value=None)):
                with self.assertRaises(lease.Rejected):
                    sftp.stop(root)
                self.assertFalse(sftp.server_running(92, 40222, proc))
                self.assertFalse(sftp.server_running(91, 40999, proc))
                (proc / "91" / "cmdline").write_bytes(b"sleep\x00100\x00")  # a reused pid is not the server
                sftp.stop(root)

    def test_forward_option_binds_the_host_loopback_only(self):
        holder, root = run_directory()
        with holder:
            self.receipt(root)
            self.assertEqual(sftp.forward_option(root), "-R 127.0.0.1:40222:127.0.0.1:40222")
            self.assertEqual(sftp.forward_option(root, 2222), "-R 127.0.0.1:2222:127.0.0.1:40222")
            for port in (0, 80, 70000):
                with self.assertRaises(lease.Rejected):
                    sftp.forward_option(root, port)

    def test_receipt_rejects_foreign_unit_names(self):
        holder, root = run_directory()
        with holder:
            lease.write_private(root / sftp.RECEIPT, {"schema": 1, "unit": "sshd.service", "pid": 1, "address": "127.0.0.1", "port": 40222})
            with self.assertRaises(lease.Rejected):
                sftp.read_receipt(root)


if __name__ == "__main__":
    unittest.main()
