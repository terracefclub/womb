import io
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
    def test_confinement_shape_of_the_server_and_unit(self):
        holder, root = run_directory()
        with holder:
            command = sftp.server_command(root, RCLONE, 40222)
            self.assertEqual(command[:4], [str(RCLONE), "serve", "sftp", ":local:/var/empty"])
            self.assertEqual(command[command.index("--addr") + 1], "127.0.0.1:40222")
            self.assertEqual(command.count("--key"), 1)
            for forbidden in ("--no-auth", "--user", "--pass", "--links", "--vfs-links", "-L", "--copy-links", "--auth-proxy", "--stdio"):
                self.assertNotIn(forbidden, command)
            self.assertEqual(command[command.index("--vfs-cache-mode") + 1], "off")
            properties = sftp.unit_properties(root, "u", RCLONE, 40222, 600)
            joined = "\n".join(properties)
            for expected in ("RuntimeMaxSec=600", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=tmpfs", "PrivateTmp=yes",
                             "RestrictAddressFamilies=AF_INET AF_UNIX", "TemporaryFileSystem=", "/var/empty/app", "KillMode=control-group"):
                self.assertIn(expected, joined)
            # Only the repository is writable; the keys are read-only binds.
            self.assertIn("BindReadOnlyPaths=" + str(root.resolve() / sftp.HOST_KEY), joined)
            self.assertNotIn(str(root.resolve() / sftp.HOST_KEY) + ":", [line.split("=", 1)[1] for line in properties if line.startswith("--property=BindPaths")][0])

    def test_unsafe_paths_rejected(self):
        for bad in ("relative", "/a/../b", "/a b", "/a;b", "/a$b"):
            with self.assertRaises(lease.Rejected):
                sftp.safe_path(bad)

    def test_start_preflight(self):
        holder, root = run_directory()
        with holder:
            with patch.object(Path, "is_file", return_value=True):
                for seconds in (0, 299, 21601):
                    with self.assertRaises(lease.Rejected):
                        sftp.start(root, RCLONE, seconds)
                with self.assertRaises(lease.Rejected):
                    sftp.start(root, Path("/usr/bin/rclone"), 600)
                (root / sftp.RECEIPT).touch()
                with self.assertRaises(lease.Rejected):
                    sftp.start(root, RCLONE, 600)

    def test_free_port_is_unprivileged_loopback(self):
        self.assertGreaterEqual(sftp.free_port(), 1024)


class StopTest(unittest.TestCase):
    def receipt(self, root):
        lease.write_private(root / sftp.RECEIPT, {"schema": 1, "unit": "terrace-lease-sftp-" + "0" * 16 + ".service", "pid": 0,
                                                  "address": "127.0.0.1", "port": 40222})

    def test_stop_requires_inactive_unit_and_silent_listener(self):
        holder, root = run_directory()
        with holder:
            self.receipt(root)
            gone = {"ActiveState": "inactive", "MainPID": "0", "ControlPID": "0", "ControlGroup": ""}
            with patch.object(sftp, "systemctl") as systemctl, patch.object(sftp, "properties", return_value=gone), patch.object(sftp, "banner", return_value=None):
                sftp.stop(root)
                self.assertEqual(systemctl.call_args.args[:2], ("stop", "terrace-lease-sftp-" + "0" * 16 + ".service"))
                self.assertTrue((root / sftp.STOPPED).exists())
                sftp.stop(root)  # idempotent
            (root / sftp.STOPPED).unlink()
            for facts, answer in (({**gone, "ActiveState": "active"}, None), ({**gone, "MainPID": "42"}, None),
                                  ({**gone, "ControlGroup": "/x"}, None), (gone, True)):
                with patch.object(sftp, "systemctl"), patch.object(sftp, "properties", return_value=facts), patch.object(sftp, "banner", return_value=answer):
                    with self.assertRaises(lease.Rejected):
                        sftp.stop(root)
            self.assertFalse((root / sftp.STOPPED).exists())

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
