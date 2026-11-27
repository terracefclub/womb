import io
import json
import os
from pathlib import Path
import re
import tempfile
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
            command = sftp.watcher_command(root, "terrace-lease-sftp-" + "1" * 16 + ".service", 1048576)
            self.assertEqual(command[1:3], ["-B", str(Path(sftp.__file__).resolve())])
            self.assertEqual(command[3:], ["watch", "--directory", run, "--unit", "terrace-lease-sftp-" + "1" * 16 + ".service",
                                           "--limit-bytes", "1048576", "--interval", "2"])

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


class WatchTest(unittest.TestCase):
    UNIT = "terrace-lease-sftp-" + "2" * 16 + ".service"

    def test_stops_the_server_over_the_limit_and_records_it(self):
        holder, root = run_directory()
        with holder:
            (root / sftp.REPOSITORY).mkdir()
            (root / sftp.LOGS).mkdir()
            (root / sftp.REPOSITORY / "pack").write_bytes(b"x" * 100)

            def sleeper(_):
                (root / sftp.REPOSITORY / "pack").write_bytes(b"x" * 5000)
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 1000, 2, sleep=sleeper))
                self.assertEqual(systemctl.call_args.args[:2], ("stop", self.UNIT))
            self.assertEqual(json.loads((root / sftp.TRIPPED).read_text())["limitBytes"], 1000)

    def test_counts_the_log_and_ends_with_the_server(self):
        holder, root = run_directory()
        with holder:
            (root / sftp.REPOSITORY).mkdir()
            (root / sftp.LOGS).mkdir()
            (root / sftp.LOGS / "rclone.log").write_bytes(b"x" * 2000)
            with patch.object(sftp, "properties", return_value={"ActiveState": "active"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertTrue(sftp.watch(root, self.UNIT, 1000, 2, sleep=lambda _: None))
                self.assertTrue(systemctl.called)
            (root / sftp.TRIPPED).unlink()
            states = iter([{"ActiveState": "active"}, {"ActiveState": "inactive"}])
            (root / sftp.LOGS / "rclone.log").write_bytes(b"x")
            with patch.object(sftp, "properties", side_effect=lambda *a: next(states)), patch.object(sftp, "systemctl") as systemctl:
                self.assertFalse(sftp.watch(root, self.UNIT, 1000, 2, sleep=lambda _: None))
                self.assertFalse(systemctl.called)

    def test_gives_up_when_the_server_never_appears_and_refuses_foreign_units(self):
        holder, root = run_directory()
        with holder:
            (root / sftp.REPOSITORY).mkdir()
            (root / sftp.LOGS).mkdir()
            with patch.object(sftp, "properties", return_value={"ActiveState": "inactive"}), patch.object(sftp, "systemctl") as systemctl:
                self.assertFalse(sftp.watch(root, self.UNIT, 1000, 30, sleep=lambda _: None))
                self.assertFalse(systemctl.called)
                for unit in ("sshd.service", self.UNIT[:-8] + "-watch.service"):
                    with self.assertRaises(lease.Rejected):
                        sftp.watch(root, unit, 1000, 2)


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
