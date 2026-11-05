"""Synthetic ownership/error regressions; no host unit, VM or provider call."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import host


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_private_run_receipt_and_identity(self):
        run, run_id = host.create_run(self.parent)
        self.assertEqual(run.name, "run-" + run_id)
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        self.assertEqual((run / "receipt.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(json.loads((run / "receipt.json").read_text())["complete"])

    def test_insecure_parent_rejected(self):
        self.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            host.create_run(self.parent)

    def test_unrelated_unit_rejected_before_any_command(self):
        with patch.object(host.subprocess, "run") as call:
            with self.assertRaises(ValueError):
                host.unit_state("unrelated.service")
            call.assert_not_called()

    def test_query_error_is_not_treated_as_absence(self):
        with patch.object(host.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "PRIVATE_ERROR_MARKER")):
            with self.assertRaises(RuntimeError):
                host.unit_state("terrace-rehearsal-" + "a" * 32 + ".service")

    def test_stop_error_propagates(self):
        with patch.object(host, "unit_off", return_value=False), patch.object(host, "command", side_effect=subprocess.CalledProcessError(1, ["synthetic"])):
            with self.assertRaises(subprocess.CalledProcessError):
                host.stop_unit("terrace-rehearsal-" + "a" * 32 + ".service", None)

    def runtime(self, run, run_id, directory):
        host.write_json(run / "runtime.json", {"scope": "terrace-disposable-synthetic", "runId": run_id, "directory": str(directory)})

    def test_stale_runtime_symlink_preserves_target(self):
        run, run_id = host.create_run(self.parent)
        target = self.parent / "unrelated"
        target.mkdir()
        (target / "sentinel").write_text("retained")
        link = Path("/tmp") / ("tr-" + run_id[:8] + "-synthetic-link")
        try:
            link.symlink_to(target)
            self.runtime(run, run_id, link)
            with self.assertRaises(ValueError):
                host.remove_probe_runtime(run, run_id)
            self.assertEqual((target / "sentinel").read_text(), "retained")
        finally:
            link.unlink(missing_ok=True)

    def test_other_run_marker_rejected(self):
        run, run_id = host.create_run(self.parent)
        with tempfile.TemporaryDirectory(prefix="tr-" + run_id[:8] + "-", dir="/tmp") as directory:
            self.runtime(run, run_id, Path(directory))
            host.write_json(Path(directory) / "owner.json", {"scope": "terrace-disposable-synthetic", "runId": "other"})
            with patch.object(host, "unit_off", return_value=True), self.assertRaises(ValueError):
                host.remove_probe_runtime(run, run_id)
            self.assertTrue(Path(directory).is_dir())

    def test_exact_probe_runtime_removed_only_when_off(self):
        run, run_id = host.create_run(self.parent)
        directory = Path(tempfile.mkdtemp(prefix="tr-" + run_id[:8] + "-", dir="/tmp"))
        try:
            self.runtime(run, run_id, directory)
            host.write_json(directory / "owner.json", {"scope": "terrace-disposable-synthetic", "runId": run_id})
            with patch.object(host, "unit_off", return_value=False), self.assertRaises(ValueError):
                host.remove_probe_runtime(run, run_id)
            with patch.object(host, "unit_off", return_value=True):
                host.remove_probe_runtime(run, run_id)
            self.assertFalse(directory.exists())
        finally:
            if directory.exists():
                import shutil
                shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
