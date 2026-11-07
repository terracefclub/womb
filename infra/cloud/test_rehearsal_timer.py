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
                           "NextElapseUSecRealtime": "Thu " + timer.calendar(2000), "User": "operator",
                           "AccuracyUSec": "1s", "RemainAfterElapse": "no", "KillMode": "control-group",
                           "Type": "exec", "RuntimeMaxUSec": "7min", "TimeoutStopUSec": "10s",
                           "Restart": "on-failure", "RestartUSec": "20s", "StartLimitIntervalUSec": "2h",
                           "StartLimitBurst": "3", "UMask": "0077", "NoNewPrivileges": "yes", "PrivateTmp": "yes",
                           "ProtectSystem": "strict", "ReadWritePaths": str(self.directory),
                           "ExecStart": "{ path=" + self.argv[0] + " ; argv[]=" + " ".join(self.argv)
                                        + " ; ignore_errors=no ; pid=0 }"}

    def ready(self, now=1000):
        def read_properties(unit, keys):
            return {key: "inactive" if unit.endswith(".service") and key == "ActiveState"
                    else self.properties[key] for key in keys}

        with (patch.object(timer, "timer_settings", return_value=(self.unit, "operator", self.argv)),
              patch.object(timer, "TRANSIENT", self.directory), patch.object(timer, "read_properties", read_properties)):
            timer.check_ready(self.directory, self.manifest, self.credential, now=now)

    def arm_receipt(self):
        lease.write_private(self.directory / "timer-armed.json",
                            {**self.manifest, "scriptSha256": hashlib.sha256(self.script.read_bytes()).hexdigest()})

    def test_ready_requires_exact_service_timer_source_and_expiry(self):
        with self.assertRaises(FileNotFoundError):
            self.ready()
        self.arm_receipt()
        self.ready()
        for property_name in ("SubState", "Unit", "ExecStart", "KillMode", "RuntimeMaxUSec", "NextElapseUSecRealtime"):
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

    def test_disarm_requires_empty_or_absent_kernel_cgroup(self):
        lease.write_private(self.directory / "closed.json", {"closedAt": 1200})
        lease.write_private(self.directory / "final-absence-1.json", {
            "run": self.manifest["run"], "phase": "final", "baselinePreserved": True, "absenceReads": 3,
        })
        cgroup = self.directory / (self.unit + ".service")
        cgroup.mkdir()
        with (patch.object(timer, "timer_settings", return_value=(self.unit, "operator", self.argv)),
              patch.object(timer, "CGROUP", self.directory), patch.object(timer, "system"),
              patch.object(timer, "read_property", side_effect=lambda _unit, key: "inactive" if key == "ActiveState" else "")):
            with self.assertRaises(lease.Rejected):
                timer.disarm(self.directory, self.manifest, self.credential)
            (cgroup / "cgroup.events").write_text("populated 1\nfrozen 0\n")
            with self.assertRaises(lease.Rejected):
                timer.disarm(self.directory, self.manifest, self.credential)
            (cgroup / "cgroup.events").write_text("populated 0\nfrozen 0\n")
            timer.disarm(self.directory, self.manifest, self.credential)


if __name__ == "__main__":
    unittest.main()
