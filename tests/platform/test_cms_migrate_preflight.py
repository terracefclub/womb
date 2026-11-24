"""The migration's last start-up step (platform/cms-migrate-preflight.sh), with a stub systemctl.

The module builds it with writeShellApplication (errexit, nounset, pipefail); this test runs it the same way. The
website VM proof runs the real one: a refused migration leaves the CMS and website running.
Run: python3 -B tests/platform/test_cms_migrate_preflight.py -v
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(os.environ.get("CMS_MIGRATE_PREFLIGHT", Path(__file__).resolve().parents[2] / "platform/cms-migrate-preflight.sh"))
BASH = shutil.which("bash") or "/bin/bash"
TOOLS_PATH = ":".join(sorted({os.path.dirname(BASH), *(os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("cut", "tr")),}))
UNITS = ["terrace-cms.service", "terrace-web.service", "terrace-cms-sweep.service"]


class Preflight(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        (self.dir / "bin").mkdir()
        self.calls = self.dir / "calls"
        stub = self.dir / "bin" / "systemctl"
        # list-units prints FAKE_UNITS (or fails with FAKE_LIST_FAILS); show prints the unit's state from FAKE_<NAME>; stop is recorded.
        stub.write_text(f"#!{BASH}\n"
                        'echo "$*" >>"$FAKE_CALLS"\n'
                        'case "$1" in\n'
                        '  list-units) [[ -z "${FAKE_LIST_FAILS:-}" ]] || exit 1; printf "%b" "$FAKE_UNITS" ;;\n'
                        '  show) [[ "$3 $4" == "-p ActiveState" && "$5" == "--value" ]] || exit 99; v="FAKE_${2//[-.]/_}"; echo "${!v:-inactive}" ;;\n'
                        '  stop) ;;\n'
                        '  *) exit 99 ;;\n'
                        'esac\n')
        stub.chmod(0o700)

    def run_preflight(self, units="", list_fails="", **states):
        env = {"PATH": f"{self.dir / 'bin'}:{TOOLS_PATH}", "FAKE_CALLS": str(self.calls), "FAKE_UNITS": units, "FAKE_LIST_FAILS": list_fails}
        for unit, state in states.items():
            env["FAKE_" + unit.replace("-", "_").replace(".", "_")] = state
        return subprocess.run([BASH, "-euo", "pipefail", str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)

    def calls_made(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_refuses_before_stopping_anything_while_a_tool_unit_is_active(self):
        units = "terrace-cms-tool-roster-invite-1 loaded active running Terrace CMS operator tool (roster-invite)\\n"
        result = self.run_preflight(units=units, terrace_cms_service="active", terrace_web_service="active")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "A CMS operator tool is running (terrace-cms-tool-roster-invite-1); migration refused before anything was stopped. "
                                        "Start the migration after it has finished.\n")
        self.assertFalse(any(call.startswith(("stop", "show")) for call in self.calls_made()), self.calls_made())

    def test_refuses_when_the_tool_units_cannot_be_listed(self):
        result = self.run_preflight(list_fails="1", terrace_cms_service="active")
        self.assertEqual((result.returncode, result.stderr), (1, "Cannot list CMS operator tool units; migration refused before anything was stopped.\n"))
        self.assertFalse(any(call.startswith("stop") for call in self.calls_made()))

    def test_stops_only_the_units_that_are_running(self):
        result = self.run_preflight(terrace_cms_service="active", terrace_web_service="activating", terrace_cms_sweep_service="inactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in self.calls_made() if call.startswith("stop")], ["stop terrace-cms.service terrace-web.service"])

    def test_a_sweep_queued_behind_the_migration_is_not_cancelled(self):
        # Inactive (its start job waits for the migration): not stopped, so systemd keeps the queued start.
        result = self.run_preflight(terrace_cms_service="active", terrace_web_service="active", terrace_cms_sweep_service="inactive")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("terrace-cms-sweep.service", " ".join(call for call in self.calls_made() if call.startswith("stop")))

    def test_every_running_state_is_stopped_and_nothing_when_all_are_down(self):
        for state in ["active", "activating", "deactivating", "reloading"]:
            self.calls.unlink(missing_ok=True)
            self.assertEqual(self.run_preflight(terrace_cms_sweep_service=state).returncode, 0)
            self.assertEqual([call for call in self.calls_made() if call.startswith("stop")], ["stop terrace-cms-sweep.service"], state)
        self.calls.unlink(missing_ok=True)
        self.assertEqual(self.run_preflight(terrace_cms_service="failed", terrace_web_service="inactive").returncode, 0)
        self.assertEqual([call for call in self.calls_made() if call.startswith("stop")], [])


if __name__ == "__main__":
    unittest.main()
