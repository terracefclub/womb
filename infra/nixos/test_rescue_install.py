import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import rescue_install as rescue
import stock_kexec
from test_stock_kexec_native import capacity_facts, cloud_status_facts, diagnostic_command
from test_rehearsal_lease import RUN, write_reviewed_input


class RescueInstallTest(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.directory = Path(self.private.name)

    def write(self, name, data):
        path = self.directory / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_binding_rejects_stale_baseline_or_wrong_role(self):
        run = "a" * 32
        self.write("lease.json", {"schema": 1, "run": run, "expires": 9000,
                                  "baseline": {"servers": {"41": {}}}})
        self.write("original-created.json", {"servers": [41, 42]})
        binding = {"run": run, "phase": "original", "role": "app", "serverId": 42,
                   "ipv4": "1.1.1.1", "expires": 8000, "observedAt": 1000}
        self.write("original-app-binding-1.json", binding)
        self.assertEqual(rescue.lease_binding(self.directory, "original", "app", 1100)["serverId"], 42)
        for changes, now in [({}, 1121), ({"serverId": 41}, 1100), ({"serverId": 43}, 1100),
                             ({"role": "relay"}, 1100), ({"expires": 1099}, 1100),
                             ({"ipv4": "127.0.0.1"}, 1100)]:
            with self.subTest(changes=changes, now=now):
                self.write("original-app-binding-10.json", {**binding, **changes})
                with self.assertRaises(rescue.Rejected):
                    rescue.lease_binding(self.directory, "original", "app", now)

    def test_closed_receipt_blocks_stage_even_with_valid_lease(self):
        run = "a" * 32
        self.write("lease.json", {"schema": 1, "run": run, "expires": 9000})
        with patch.object(rescue.time, "time", return_value=1000):
            rescue.require_open(self.directory, run, 8000)
            self.assertEqual(rescue.remaining(self.directory, {"run": run, "expires": 1500}, 1800), 480)
            with self.assertRaises(rescue.Rejected):
                rescue.require_open(self.directory, run, 999)
            self.write("closed.json", {})
            with self.assertRaises(rescue.Rejected):
                rescue.require_open(self.directory, run, 8000)

    def test_rescue_pin_must_match_console_and_exact_host(self):
        blob = rescue.KEY_PREFIX + b"x" * 32
        encoded = base64.b64encode(blob)
        fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
        known = b"1.1.1.1 ssh-ed25519 " + encoded + b"\n"
        self.assertEqual(rescue.known_key(known, "1.1.1.1", fingerprint), encoded.decode())
        for address, expected in [("8.8.8.8", fingerprint), ("1.1.1.1", b"SHA256:wrong")]:
            with self.assertRaises(rescue.Rejected):
                rescue.known_key(known, address, expected)

    def test_stock_pin_must_match_dispatched_key_before_ssh(self):
        blob = rescue.KEY_PREFIX + b"x" * 32
        encoded = base64.b64encode(blob).decode()
        fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
        for name, contents in [
            ("original-relay-bootstrap-known-hosts", f"1.1.1.1 ssh-ed25519 {encoded}\n".encode()),
            ("original-relay-bootstrap-fingerprint", fingerprint),
            ("operator-ed25519", b"fixture"),
        ]:
            path = self.directory / name
            path.write_bytes(contents)
            path.chmod(0o600)
        binding = {"ipv4": "1.1.1.1"}
        with patch.object(rescue, "lease_binding", return_value=binding), patch.object(
            rescue, "reviewed_bootstrap", return_value="ssh-ed25519 " + encoded
        ) as reviewed:
            _, key, options, ssh = rescue.bootstrap_authority(self.directory, "original", "relay")
            self.assertEqual(key, encoded)
            self.assertIn("StrictHostKeyChecking=yes", options)
            self.assertTrue(any(x.endswith("original-relay-bootstrap-known-hosts") for x in options))
            self.assertEqual(ssh[-1], "root@1.1.1.1")
            reviewed.return_value = "ssh-ed25519 another"
            with self.assertRaisesRegex(rescue.Rejected, "reviewed host key"):
                rescue.bootstrap_authority(self.directory, "original", "relay")

    def test_stock_published_key_accepts_one_exact_line_with_or_without_lf(self):
        path = self.directory / "host.pub"
        lines = stock_kexec.STOCK_PROBE.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("mapfile -t -n 2 published "))
        script = 'set -euo pipefail\nkeys=(unused "$2")\n' + "\n".join(lines[start:start + 3])
        key = b"ssh-ed25519 synthetic"
        for data, expected in [(key, 0), (key + b"\n", 0), (key + b"\n\n", 1),
                               (key + b" comment\n", 1), (key + b"\nssh-ed25519 other\n", 1)]:
            path.write_bytes(data)
            result = subprocess.run(["bash", "-c", script, "fixture", "synthetic", str(path)], timeout=5)
            self.assertEqual(result.returncode, expected)

    def test_stock_dispatch_checks_archive_and_rebinds_before_kexec(self):
        archive = self.directory / "installer.tar.gz"
        self.stock_archive(archive)
        digest = rescue.sha(archive.read_bytes())
        binding = {"run": RUN, "ipv4": "1.1.1.1"}
        boot = "abcd0123-1234-1234-1234-1234567890ab"
        other = {**binding, "ipv4": "8.8.8.8"}
        initial = (binding, "fixture", [], ["ssh"])
        with patch.object(rescue, "bootstrap_authority", return_value=initial) as authority, patch.object(
            stock_kexec, "stock_boot", return_value=boot
        ), patch.object(rescue, "remaining", return_value=60), patch.object(rescue, "remote") as remote:
            with self.assertRaisesRegex(rescue.Rejected, "archive rejected"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, "0" * 64)
            remote.assert_not_called()
            authority.side_effect = [initial, (other, "fixture", [], ["ssh"])]
            with self.assertRaisesRegex(rescue.Rejected, "target changed"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(remote.call_args.args[2], stock_kexec.UPLOAD)
            authority.side_effect = None
            stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)
            self.assertEqual(remote.call_args.args[2], stock_kexec.KEXEC)
            sent, _ = rescue.read_json(self.directory / "original-relay-kexec-dispatched.json")
            self.assertEqual(sent["archiveUnpackedBytes"], len(stock_kexec.ARCHIVE_FILES))
            with self.assertRaisesRegex(rescue.Rejected, "already dispatched"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)

    def stock_archive(self, path, names=None):
        with tarfile.open(path, "w:gz") as archive:
            root = tarfile.TarInfo("kexec")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for name in sorted(stock_kexec.ARCHIVE_FILES) if names is None else names:
                entry = tarfile.TarInfo(name)
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))

    def test_stock_archive_requires_exact_entries_and_measures_unpacked_size(self):
        path = self.directory / "installer.tar.gz"
        self.stock_archive(path)
        with path.open("rb") as stream:
            self.assertEqual(stock_kexec.archive_unpacked_bytes(stream), 5)
        for extra in ("kexec/run", "kexec/extra", "../escaping"):
            self.stock_archive(path, [*stock_kexec.ARCHIVE_FILES, extra])
            with path.open("rb") as stream, self.assertRaisesRegex(rescue.Rejected, "unexpected installer"):
                stock_kexec.archive_unpacked_bytes(stream)
        self.stock_archive(path, ["kexec/run"])
        with path.open("rb") as stream, self.assertRaisesRegex(rescue.Rejected, "entry missing"):
            stock_kexec.archive_unpacked_bytes(stream)

    def test_native_diagnostic_pipes_are_capped_and_timed(self):
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], 5
        ), (0, b"out\n", b"err\n"))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "print('x' * 8192)"], 5
        ), (125, b"", b""))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "print('x' * 8192)"], 5, truncate=True
        ), (0, b"x" * 8192, b""))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "import time; time.sleep(10)"], 0.05
        ), (124, b"", b""))
        with tempfile.TemporaryFile() as data:
            data.write(b"exact synthetic input")
            data.seek(0)
            self.assertEqual(diagnostic_command(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                5, stdin=data,
            ), (0, b"exact synthetic input", b""))

    def test_cloud_status_keeps_only_known_states_and_counts(self):
        status = {"status": "done", "extended_status": "degraded done", "errors": [],
                  "recoverable_errors": {"WARNING": ["synthetic private message"]}}
        self.assertEqual(cloud_status_facts(2, json.dumps(status)), {
            "statusReadRc": 2, "status": "done", "extendedStatus": "degraded done",
            "fatalErrorCount": 0, "recoverableErrorCount": 1,
        })
        self.assertIsNone(cloud_status_facts(1, b'{"status":"synthetic private message"}')["status"])
        self.assertIsNone(cloud_status_facts(125, b"")["recoverableErrorCount"])

    def test_upload_capacity_diagnostics_keep_only_flags_and_numbers(self):
        self.assertEqual(capacity_facts(b"tmpfs\nfixture-type\n12\n34\n56\n"), {
            "runTmpfs": True, "shmTmpfs": False, "runFreeBytes": 12,
            "shmFreeBytes": 34, "memAvailableKiB": 56,
        })
        self.assertIsNone(capacity_facts(b"tmpfs\ntmpfs\nraw-private\n34\n56\n"))

    def test_upload_failure_cleanup_targets_its_exact_leaf(self):
        leaf = self.directory / "owned-upload"
        leaf.mkdir()
        cleanup = "cleanup() {" + stock_kexec.UPLOAD.split("cleanup() {", 1)[1].split("trap cleanup", 1)[0]
        script = """
set -euo pipefail
scratch=$1
mountpoint() { test "$3" = "$scratch"; }
findmnt() { test "${@: -1}" = "$scratch"; printf 'tmpfs\\n'; }
umount() { test "$2" = "$scratch"; printf '%s\\n' "$2"; }
""" + cleanup + 'trap cleanup EXIT\ncd "$scratch"\nexit 7'
        result = subprocess.run(["bash", "-c", script, "fixture", str(leaf)], capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (7, (str(leaf) + "\n").encode()))
        self.assertFalse(leaf.exists())

    def test_ram_receipt_binds_new_boot_and_stock_dispatch(self):
        old = "abcd0123-1234-1234-1234-1234567890ab"
        new = "abcd0123-1234-1234-1234-1234567890cd"
        binding = {"run": RUN, "ipv4": "1.1.1.1"}
        dispatch = self.write("original-relay-kexec-dispatched.json", {
            "binding": binding, "bootstrapKey": "fixture", "stockBootId": old,
        })
        value = (binding, "fixture", [], ["ssh"])
        with patch.object(rescue, "bootstrap_authority", return_value=value), patch.object(
            rescue, "ram_boot_id", return_value=old
        ) as boot:
            with self.assertRaisesRegex(rescue.Rejected, "stock boot is still running"):
                stock_kexec.verify_ram(self.directory, "original", "relay")
            boot.return_value = new
            stock_kexec.verify_ram(self.directory, "original", "relay")
            self.assertEqual(rescue.authority(self.directory, "original", "relay", ram=True), value)
            boot.return_value = old
            with self.assertRaisesRegex(rescue.Rejected, "RAM installer changed"):
                rescue.authority(self.directory, "original", "relay", ram=True)
            boot.return_value = new
            dispatch.write_bytes(dispatch.read_bytes() + b" ")
            with self.assertRaisesRegex(rescue.Rejected, "RAM installer changed"):
                rescue.authority(self.directory, "original", "relay", ram=True)

    def test_console_probe_cannot_dispatch_an_install(self):
        cfg = {"run": RUN, "phase": "original", "probeOnly": True}
        path = write_reviewed_input(self.directory, cfg)
        with patch.object(rescue, "authority") as authority:
            with self.assertRaisesRegex(rescue.Rejected, "cannot write a disk"):
                rescue.install(self.directory, "original", "relay", "unused", self.directory / "archive")
            path.unlink()
            self.write("original-input.json", {"run": RUN, "phase": "original"})
            with self.assertRaisesRegex(rescue.Rejected, "no longer matches"):
                rescue.install(self.directory, "original", "relay", "unused", self.directory / "archive")
            authority.assert_not_called()

    def test_disk_probe_rejects_mount_holder_and_partition(self):
        row = {"path": "/dev/vda", "type": "disk", "size": 123456, "ro": False,
               "rm": False, "maj:min": "252:0", "mountpoints": [None]}
        part = {**row, "path": "/dev/vda1", "type": "part"}

        def facts(disk):
            return ("abcd0123-1234-1234-1234-1234567890ab\n/dev/vda\n"
                    + json.dumps({"blockdevices": [disk]})).encode()

        alias = "/dev/disk/by-id/scsi-test"
        self.assertEqual(rescue.parse_probe(facts({**row, "children": [part]}), alias)["canonical"], "/dev/vda")
        for changed in [{**row, "children": [{**part, "mountpoints": ["/mnt"]}]},
                        {**row, "children": [{**part, "children": [row]}]},
                        {**row, "type": "part"}, {**row, "rm": True}]:
            with self.assertRaises(rescue.Rejected):
                rescue.parse_probe(facts(changed), alias)
        for bad in ["/dev/vda1", "/dev/disk/by-id/scsi-test-part1", "/dev/../dev/vda", "/dev/nvme0n1p1"]:
            self.assertFalse(rescue.valid_disk(bad))

    def test_approved_plan_must_match_disk_and_exact_bytes(self):
        store = "/nix/store/" + "a" * 32 + "-"
        plan = {"diskSha256": "b" * 64, "disko": store + "disko",
                "installer": store + "installer", "system": store + "system"}
        path = self.write("original-app-install.json", plan)
        digest = rescue.sha(path.read_bytes())
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(rescue.checked_plan(self.directory, "original-app", digest, plan["diskSha256"]), plan)
            with self.assertRaises(rescue.Rejected):
                rescue.checked_plan(self.directory, "original-app", digest, "c" * 64)
            self.write(path.name, {**plan, "system": store + "changed"})
            with self.assertRaises(rescue.Rejected):
                rescue.checked_plan(self.directory, "original-app", digest, plan["diskSha256"])

    def test_copy_targets_mounted_store_with_strict_ssh_options(self):
        options = ["-o", "StrictHostKeyChecking=yes"]
        with patch.object(rescue, "checked_process") as checked:
            rescue.copy_store({"ipv4": "1.1.1.1"}, options, 10, ["/nix/store/reviewed"], target=True)
        args, _seconds = checked.call_args.args
        self.assertIn("remote-store=local%3Froot%3D%2Fmnt", args[3])
        self.assertEqual(checked.call_args.kwargs["env"]["NIX_SSHOPTS"], "-o StrictHostKeyChecking=yes")

    def test_all_fixed_remote_scripts_parse_without_execution(self):
        for script in [rescue.PROBE, rescue.BOOTSTRAP, rescue.FORMAT, rescue.FINISH,
                       rescue.RAM_PROBE, rescue.RAM_SERVE,
                       stock_kexec.STOCK_PROBE, stock_kexec.UPLOAD, stock_kexec.KEXEC]:
            subprocess.run(["bash", "-n"], input=script, text=True, check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
