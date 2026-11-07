import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import rescue_install as rescue


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
        for script in [rescue.PROBE, rescue.BOOTSTRAP, rescue.FORMAT, rescue.FINISH]:
            subprocess.run(["bash", "-n"], input=script, text=True, check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
