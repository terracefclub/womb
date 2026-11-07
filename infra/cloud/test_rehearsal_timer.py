import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import rehearsal_lease as lease
import rehearsal_timer as timer


class TimerTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)
        self.manifest = {"run": "a" * 32, "expires": 2000}
        self.credential = self.directory / "credential"
        self.script = self.directory / "rehearsal_lease.py"
        lease.write_private(self.script, {"synthetic": True})
        self.unit = "terrace-cloud-" + self.manifest["run"]
        self.argv = ["/bin/python3", "-B", str(self.script), "cleanup"]
        self.timer_unit = self.directory / (self.unit + ".timer")
        self.timer_unit.write_text("OnCalendar=" + timer.calendar(2000) + "\n")
        self.properties = {"ActiveState": "active", "SubState": "waiting", "Unit": self.unit + ".service",
                           "NextElapseUSecRealtime": "scheduled", "User": "operator", "KillMode": "control-group",
                           "ExecStart": "{ argv[]=" + " ".join(self.argv) + " ; }"}

    def ready(self, now=1000):
        def read_property(unit, key):
            if unit.endswith(".service") and key == "ActiveState":
                return "inactive"
            return self.properties[key]

        with (patch.object(timer, "timer_settings", return_value=(self.unit, "operator", self.argv)),
              patch.object(timer, "TRANSIENT", self.directory), patch.object(timer, "read_property", read_property)):
            timer.check_ready(self.directory, self.manifest, self.credential, now=now)

    def arm_receipt(self):
        lease.write_private(self.directory / "timer-armed.json",
                            {**self.manifest, "scriptSha256": hashlib.sha256(self.script.read_bytes()).hexdigest()})

    def test_ready_requires_exact_service_timer_source_and_expiry(self):
        with self.assertRaises(FileNotFoundError):
            self.ready()
        self.arm_receipt()
        self.ready()
        for property_name in ("SubState", "Unit", "ExecStart", "KillMode"):
            saved = self.properties[property_name]
            self.properties[property_name] = "unexpected"
            with self.assertRaises(lease.Rejected):
                self.ready()
            self.properties[property_name] = saved
        with self.assertRaises(lease.Rejected):
            self.ready(now=1700)
        self.script.write_text("changed\n")
        with self.assertRaises(lease.Rejected):
            self.ready()

    def test_closed_lease_never_reopens_create_window(self):
        self.arm_receipt()
        lease.write_private(self.directory / "closed.json", {"closedAt": 1200})
        with self.assertRaises(lease.Rejected):
            self.ready()

    def test_disarm_refuses_without_final_absence(self):
        with patch.object(timer, "system") as system:
            for closed in (False, True):
                if closed:
                    lease.write_private(self.directory / "closed.json", {"closedAt": 1200})
                with self.assertRaises(lease.Rejected):
                    timer.disarm(self.directory, self.manifest, self.credential)
            system.assert_not_called()


if __name__ == "__main__":
    unittest.main()
