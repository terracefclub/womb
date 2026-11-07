import copy
import json
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import rehearsal_services as services


class ServiceProofTests(unittest.TestCase):
    def test_replacement_requires_retired_original_identities(self):
        run = "a" * 32
        original = {"servers": [4, 6], "primary_ips": [8, 10], "firewalls": [], "ssh_keys": []}
        replacement = {"servers": [5, 7], "primary_ips": [9, 11], "firewalls": [], "ssh_keys": []}
        backup = {"run": run, "verified": True, "servers": [4, 6]}
        absence = {"run": run, "phase": "original", "baselinePreserved": True,
                   "absenceReads": 3, "unresolvedDispatches": [], "verifiedAbsent": original}
        services.check_original_absence(run, original, replacement, absence, backup)
        for delta in (
            {"run": "b" * 32}, {"unresolvedDispatches": ["original"]}, {"absenceReads": 2},
            {"verifiedAbsent": {**original, "servers": [4]}},
            {"verifiedAbsent": {**original, "ssh_keys": [1]}},
        ):
            with self.subTest(delta=delta), self.assertRaises(services.Rejected):
                services.check_original_absence(run, original, replacement, {**absence, **delta}, backup)
        with self.assertRaises(services.Rejected):
            services.check_original_absence(run, original, {**replacement, "servers": [4, 7]}, absence, backup)
        with self.assertRaises(services.Rejected):
            services.check_original_absence(run, original, replacement, absence, {**backup, "verified": False})

    def test_offhost_snapshot_inventory_is_exact(self):
        snapshots = [{"hostname": "relay", "id": "c" * 64}, {"hostname": "gateway", "id": "d" * 64}]
        result = services.check_snapshots(snapshots)
        self.assertEqual(result, {"relay": "c" * 64, "gateway": "d" * 64})
        for update in ({"hostname": "relay"}, {"hostname": "unexpected"}, {"id": "c" * 64}, {"id": "abcd"}):
            with self.subTest(update=update), self.assertRaises(services.Rejected):
                services.check_snapshots([snapshots[0], {**snapshots[1], **update}])
        with self.assertRaises(services.Rejected):
            services.check_snapshots(snapshots, {**result, "relay": "e" * 64})

    def test_deployment_commands_require_bounded_unique_native_identities(self):
        item = {"metadata": {"namespace": "kube-system", "name": "local-path"}, "spec": {"replicas": 1}}
        self.assertEqual(services.deployment_replicas(json.dumps({"items": [item]})),
                         [["kube-system", "local-path", 1]])
        for field, key, value in (("metadata", "name", "--all"), ("spec", "replicas", True)):
            malformed = copy.deepcopy(item)
            malformed[field][key] = value
            with self.assertRaises(services.Rejected):
                services.deployment_replicas(json.dumps({"items": [malformed]}))
        with self.assertRaises(services.Rejected):
            services.deployment_replicas(json.dumps({"items": [item, item]}))

    def test_private_input_shell_refuses_existing_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "target"
            data = b"synthetic fixture only\n"
            argv = ["bash", "-ceu", services.INSTALL_INPUT, "terrace", str(target), "0400", services.digest(data)]
            result = subprocess.run(argv, input=data, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)
            self.assertNotEqual(subprocess.run(argv, input=b"overwrite", capture_output=True, timeout=5).returncode, 0)
            self.assertEqual(target.read_bytes(), data)
            target.unlink()
            outside = Path(root) / "absent"
            target.symlink_to(outside)
            self.assertNotEqual(subprocess.run(argv, input=data, capture_output=True, timeout=5).returncode, 0)
            self.assertFalse(outside.exists())
            self.assertTrue(target.is_symlink())

    def test_ssh_uses_stdin_exact_reverse_socket_and_lease_budget(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            data = b"synthetic escrow payload"
            calls = []

            def run(argv, seconds, *, stdin, capture):
                self.assertEqual(stdin.read(), data)
                self.assertNotIn(data.decode(), shlex.join(argv))
                self.assertEqual(argv[:-1], ["ssh", "-F", "/dev/null", "-o", "ExitOnForwardFailure=yes",
                                            "-R", "127.0.0.1:22422:" + str(directory / "rest.sock"),
                                            "operator@192.0.2.5"])
                self.assertEqual(shlex.split(argv[-1])[:4], ["timeout", "--kill-after=10", "50", "sudo"])
                calls.append((seconds, capture))
                return b"ok"

            host = services.Host(directory, "app", {"serverId": 4},
                                 ["ssh", "-F", "/dev/null", "operator@192.0.2.5"],
                                 run, directory / "rest.sock", 1000)
            with patch.object(services.time, "time", return_value=920):
                self.assertEqual(host.run("test", "cat >/dev/null", seconds=300, capture=True,
                                          data=data, repository=True), b"ok")
                (directory / "closed.json").touch()
                with self.assertRaises(services.Rejected):
                    host.run("test", "false", data=data)
            self.assertEqual(calls, [(70, True)])

    def test_offhost_check_uses_independent_backend_before_listing(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "repository-data" / "repository").mkdir(parents=True)
            calls = []
            config = b"\xffsynthetic-ciphertext"

            def run(command, seconds, **kwargs):
                calls.append((command, seconds, kwargs))
                if command[-2:] == ["check", "--read-data"]:
                    return b""
                return json.dumps([{"hostname": "gateway", "id": "d" * 64},
                                   {"hostname": "relay", "id": "e" * 64}]).encode()

            rescue = SimpleNamespace(private_directory=lambda _: None,
                                     read_private=lambda _path, _limit: config, checked_process=run)
            verify = services.offhost_verifier(directory, rescue, time.time() + 600)
            self.assertEqual(verify(), ({"gateway": "d" * 64, "relay": "e" * 64}, services.digest(config)))
            self.assertEqual([call[0][-2:] for call in calls], [["check", "--read-data"], ["snapshots", "--json"]])
            self.assertEqual(calls[0][0][2], str(directory / "repository-data" / "repository"))
            self.assertNotIn("rest:", shlex.join(calls[0][0]))
            (directory / "closed.json").touch()
            with self.assertRaises(services.Rejected):
                verify()
            self.assertEqual(len(calls), 2)

    def test_remote_shells_parse(self):
        for name in ("INACTIVE", "BOOTSTRAP_APP", "BOOTSTRAP_RELAY", "APP_SIGNATURE",
                     "MEDIA_FIXTURE", "PVC_PROBE", "STOP_APP", "RESTORE", "COPY_RESTORED",
                     "ADOPT_POSTGRES", "WIND_ROLES", "START_APPLICATIONS", "RELAY_SIGNATURE", "INSTALL_INPUT"):
            with self.subTest(name=name):
                result = subprocess.run(["bash", "-n"], input=getattr(services, name).encode(),
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_quiescence_includes_latent_writers_and_scheduled_backup(self):
        for role, writers in (("app", services.APP_WRITERS), ("relay", services.RELAY_WRITERS)):
            calls = []
            host = SimpleNamespace(role=role, run=lambda *args: calls.append(args))
            services.inactive(host, writers)
            [(stage, script, *units)] = calls
            self.assertEqual((stage, script), ("writer-absence", services.INACTIVE))
            self.assertEqual(len(units), len(set(units)))
            for name in (*writers, *services.BACKUP_UNITS):
                self.assertIn(name + ".service", units)
            for name in services.BACKUP_UNITS:
                self.assertIn(name + ".timer", units)
            for unit in ("documenso-test-mailpit.service", "terrace-vaultwarden-migration-server.service",
                         "terrace-identity-health.timer", "cloud-rehearsal-inputs.service"):
                self.assertEqual(unit in units, role == "app")


if __name__ == "__main__":
    unittest.main()
