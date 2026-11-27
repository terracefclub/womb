import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lease_install_plan as plan
import rescue_install as rescue

BINDING = {"run": "f1" * 16, "phase": "original", "role": "app", "serverId": 22, "ipv4": "1.1.1.22",
           "expires": 9000}
DISK = {"device": "/dev/sda", "canonical": "/dev/sda", "size": 327684194304, "majorMinor": "8:0",
        "bootId": "0" * 8 + "-0000-0000-0000-" + "0" * 12}
STORE = "/nix/store/" + "a" * 32
OUTPUTS = {"schema": 1, "role": "app", "device": "/dev/sda",
           "system": STORE.replace("a", "b") + "-nixos-system-app-26.05.20261101.31ecbac",
           "disko": STORE.replace("a", "c") + "-disko",
           "installer": STORE.replace("a", "d") + "-nixos-install"}


REAL_EXISTS = Path.exists


def store_exists(path, *args, **kwargs):
    # The plan's store paths are stand-ins; everything else is checked for real.
    return str(path).startswith("/nix/store/") or REAL_EXISTS(path, *args, **kwargs)


def in_store():
    return patch.object(Path, "exists", store_exists)


def keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "root@app", "-f", str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return path.with_suffix(".pub").read_text().split()[1]


class LeaseInstallPlanTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.directory = self.root / "lease"
        self.directory.mkdir(mode=0o700)
        self.store = self.root / "store"
        self.store.mkdir()
        # As `terrace-secrets host create app --role app` leaves them: the private key staged, the public half stored.
        self.stage = self.root / "terrace-stage-app"
        self.stage.mkdir(mode=0o700)
        self.public = keygen(self.stage / plan.STAGED_KEY)
        hosts = self.root / "secrets" / "hosts" / "app"
        hosts.mkdir(parents=True)
        self.stored = hosts / "ssh_host_ed25519_key.pub"
        self.stored.write_text((self.stage / (plan.STAGED_KEY + ".pub")).read_text())
        self.rescue_key = keygen(self.root / "rescue")
        rescue.write_private(self.directory / "original-app-disk.json",
                             {"binding": BINDING, "rescueKey": self.rescue_key, "disk": DISK})
        self.outputs = self.write_outputs(OUTPUTS)

    def write_outputs(self, value, name="terrace-lease-app-install.json"):
        path = self.store / (("e" * 32) + "-" + name)
        path.write_text(json.dumps(value) + "\n")
        return path

    def record(self, **changes):
        arguments = {"directory": self.directory, "phase": "original", "role": "app", "outputs_path": self.outputs,
                     "stage": self.stage, "stored_public": self.stored, "store_dir": self.store, **changes}
        with in_store():
            return plan.record_plan(**arguments)

    def written(self):
        return sorted(path.name for path in self.directory.iterdir() if path.name != "original-app-disk.json")

    def test_plan_key_and_pin_are_the_files_rescue_install_checks(self):
        approved = self.record()
        self.assertEqual(self.written(), ["original-app-install.json", "original-app-installed-ed25519",
                                          "original-app-installed-known-hosts"])
        for name in self.written():
            info = (self.directory / name).lstat()
            self.assertEqual((stat.S_IMODE(info.st_mode), info.st_nlink), (0o600, 1), name)
        recorded, digest = rescue.read_json(self.directory / "original-app-install.json")
        _, disk_digest = rescue.read_json(self.directory / "original-app-disk.json")
        self.assertEqual(digest, approved)
        self.assertEqual(recorded, {"diskSha256": disk_digest, "disko": OUTPUTS["disko"],
                                    "installer": OUTPUTS["installer"], "system": OUTPUTS["system"]})
        # The staged private key byte for byte; the pin from the store's public half, at the receipt's address.
        self.assertEqual((self.directory / "original-app-installed-ed25519").read_bytes(),
                         (self.stage / plan.STAGED_KEY).read_bytes())
        self.assertEqual((self.directory / "original-app-installed-known-hosts").read_text(),
                         f"1.1.1.22 ssh-ed25519 {self.public}\n")

    def test_rescue_install_accepts_the_plan_and_the_installed_key(self):
        approved = self.record()
        # The install's own stages up to the first remote command: authority, disk receipt, plan, installed key.
        reached = []

        def remote(*_args, **_kwargs):
            reached.append("probe")
            raise OSError("stop before the host")

        with patch.object(rescue, "authority", return_value=(BINDING, self.rescue_key, [], ["ssh"])), \
                patch.object(rescue, "reviewed_probe_mode", return_value=False), \
                patch.object(rescue, "reviewed_bootstrap", return_value={}), \
                patch.object(rescue, "remote", remote), in_store():
            with self.assertRaises(rescue.InstallFailed) as caught:
                rescue.install(self.directory, "original", "app", approved, None)
        self.assertEqual((caught.exception.label, reached), ("probe", ["probe"]))
        # A different approval stops at the plan stage, before the key is read.
        with patch.object(rescue, "authority", return_value=(BINDING, self.rescue_key, [], ["ssh"])), \
                patch.object(rescue, "reviewed_probe_mode", return_value=False), \
                patch.object(rescue, "reviewed_bootstrap", return_value={}), \
                in_store():
            with self.assertRaises(rescue.InstallFailed) as caught:
                rescue.install(self.directory, "original", "app", "0" * 64, None)
        self.assertEqual(caught.exception.label, "plan")

    def test_a_staged_key_other_than_the_stored_one_is_refused(self):
        other = self.root / "other"
        other.mkdir(mode=0o700)
        keygen(other / plan.STAGED_KEY)
        with self.assertRaises(rescue.Rejected):
            self.record(stage=other)
        self.assertEqual(self.written(), [])

    def test_the_installer_key_as_host_key_is_refused(self):
        self.stored.write_text(f"ssh-ed25519 {self.rescue_key} root@app\n")
        stage = self.root / "rescue-stage"
        stage.mkdir(mode=0o700)
        os.link(self.root / "rescue", stage / plan.STAGED_KEY)
        with self.assertRaises(rescue.Rejected):
            # A hard link is refused as a private input already; a copy is refused as the installer's key.
            self.record(stage=stage)
        (stage / plan.STAGED_KEY).unlink()
        (stage / plan.STAGED_KEY).write_bytes((self.root / "rescue").read_bytes())
        (stage / plan.STAGED_KEY).chmod(0o600)
        with self.assertRaises(rescue.Rejected):
            self.record(stage=stage)
        self.assertEqual(self.written(), [])

    def test_a_readable_staged_key_is_refused(self):
        (self.stage / plan.STAGED_KEY).chmod(0o644)
        with self.assertRaises(rescue.Rejected):
            self.record()
        self.assertEqual(self.written(), [])

    def test_outputs_for_another_disk_role_or_shape_are_refused(self):
        changes = [{"device": "/dev/sdb"}, {"device": "/dev/sda1"}, {"role": "relay"}, {"schema": 2},
                   {"system": OUTPUTS["disko"]}, {"system": STORE + "-nixos-system-relay-26.05"},
                   {"disko": "/tmp/disko"}, {"installer": OUTPUTS["system"]}, {"extra": "field"}]
        for change in changes:
            with self.subTest(change=change):
                self.outputs = self.write_outputs({**OUTPUTS, **change}, name=f"install-{len(str(change))}.json")
                with self.assertRaises(rescue.Rejected):
                    self.record()
                self.assertEqual(self.written(), [])
        self.outputs = self.write_outputs(OUTPUTS)
        missing = dict(OUTPUTS)
        del missing["installer"]
        self.outputs = self.write_outputs(missing, name="missing.json")
        with self.assertRaises(rescue.Rejected):
            self.record()

    def test_outputs_outside_the_store_are_refused(self):
        outside = self.root / "install.json"
        outside.write_text(json.dumps(OUTPUTS))
        with self.assertRaises(rescue.Rejected):
            self.record(outputs_path=outside)
        # An out-link into the store is followed.
        link = self.root / "result"
        link.symlink_to(self.outputs)
        self.record(outputs_path=link)

    def test_existing_files_are_never_replaced(self):
        for name in ("install.json", "installed-ed25519", "installed-known-hosts"):
            with self.subTest(name=name):
                path = self.directory / f"original-app-{name}"
                path.write_text("earlier\n")
                with self.assertRaises(rescue.Rejected):
                    self.record()
                self.assertEqual(path.read_text(), "earlier\n")
                path.unlink()
        self.record()
        with self.assertRaises(rescue.Rejected):
            self.record()

    def test_a_receipt_for_another_phase_or_role_is_refused(self):
        for change in ({"phase": "replacement"}, {"role": "relay"}):
            with self.subTest(change=change):
                (self.directory / "original-app-disk.json").unlink()
                rescue.write_private(self.directory / "original-app-disk.json",
                                     {"binding": {**BINDING, **change}, "rescueKey": self.rescue_key, "disk": DISK})
                with self.assertRaises(rescue.Rejected):
                    self.record()
                self.assertEqual(self.written(), [])

    def test_failure_output_is_fixed(self):
        with patch("sys.argv", ["lease_install_plan.py", str(self.directory), "original", "app",
                                "--outputs", str(self.root / "absent"), "--host-stage", str(self.stage),
                                "--host-public", str(self.stored)]), \
                patch("sys.stderr") as stderr, patch("sys.stdout"):
            self.assertEqual(plan.main(), 1)
        self.assertEqual("".join(call.args[0] for call in stderr.write.call_args_list),
                         "Install plan rejected. Private inputs suppressed.\n")


if __name__ == "__main__":
    unittest.main()
