"""terrace-production's own logic (platform/production/terrace-production.sh), with a stub systemctl and tools.

platform/production/base.nix prepends the host's settings and builds the script with writeShellApplication (errexit,
nounset, pipefail); this test does the same with paths in a temporary directory. The stub systemctl keeps each unit's
state in a file and honours the held timers' ConditionPathExists=! the way systemd does (a start whose condition fails
leaves the timer inactive). The production VM proof runs the real script.
Run: python3 -B tests/platform/test_terrace_production.py -v
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(os.environ.get("TERRACE_PRODUCTION_SCRIPT", Path(__file__).resolve().parents[2] / "platform/production/terrace-production.sh"))
BASH = shutil.which("bash") or "/bin/bash"
TOOLS_PATH = ":".join(sorted({os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("bash", "cat", "rm", "install", "chmod", "date", "grep", "tr")}))
WRITERS = ["postgresql", "nginx", "terrace-cms"]
APP_TIMERS = ["terrace-cms-sweep", "terrace-backup", "terrace-backup-check", "terrace-backup-freshness", "terrace-identity-health"]
BACKUP = ["terrace-backup", "terrace-backup-check", "terrace-backup-freshness"]

SYSTEMCTL = r'''
state() { local f="$FAKE_UNITS/$1"; if [[ -e $f ]]; then cat "$f"; else echo inactive; fi; }
# A held timer's start is skipped while its marker exists (ConditionPathExists=!marker).
start_one() {
  local unit=$1 marker
  marker=$(grep -s "^$unit=" "$FAKE_CONDITIONS" | cut -d= -f2-)
  if [[ -n $marker && -e $marker ]]; then return 0; fi
  echo active >"$FAKE_UNITS/$unit"
}
echo "$*" >>"$FAKE_LOG"
case "$1" in
  show)
    unit=$2; shift 2
    case "$*" in
      "-p ActiveState --value") state "$unit" ;;
      "-p ConditionResult --value") echo no ;;
      "-p MainPID -p NRestarts --value") printf '1\n0\n' ;;
      "-p Result --value") echo success ;;
      *) exit 99 ;;
    esac ;;
  is-failed) exit 1 ;;
  is-active)
    if [[ $2 == --quiet ]]; then [[ $(state "$3") == active ]]; else state "$2"; [[ $(state "$2") == active ]]; fi ;;
  reset-failed) ;;
  start)
    shift
    for unit in "$@"; do
      if [[ $unit == terrace-production.target ]]; then
        echo active >"$FAKE_UNITS/$unit"
        for writer in $FAKE_WRITERS; do echo active >"$FAKE_UNITS/$writer.service"; done
        for timer in $FAKE_TIMERS; do start_one "$timer.timer"; done
      else
        start_one "$unit"
      fi
    done ;;
  *) exit 99 ;;
esac
'''


class TerraceProduction(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.bin = self.dir / "bin"
        self.units = self.dir / "units"
        self.state = self.dir / "var-lib-terrace-production"
        for path in (self.bin, self.units, self.state):
            path.mkdir()
        self.log = self.dir / "systemctl.log"
        self.conditions = self.dir / "conditions"
        stubs = {
            "id": '[[ $1 == -u ]] && echo "$FAKE_UID"\n',
            "systemctl": SYSTEMCTL,
            "terrace-secrets": '[[ $1 == verify ]] && echo "terrace-secrets verify: PASS"\n',
            "sleep": 'exit 0\n',
            # cut is used by the stub only.
            "cut": f'exec {shutil.which("cut") or "/usr/bin/cut"} "$@"\n',
        }
        for name, body in stubs.items():
            path = self.bin / name
            path.write_text(f"#!{BASH}\n" + body)
            path.chmod(0o700)
        self.restored = self.dir / "reconciliation.txt"
        self.populated = self.dir / "PG_VERSION"
        self.hold = self.state / "timers-held"
        self.service_hold = self.state / "service-timers-held"
        self.adopted = self.state / "adopted"

    def wrapper(self, timers, held, service_held):
        self.conditions.write_text("".join(f"{t}.timer={self.hold}\n" for t in held) + "".join(f"{t}.timer={self.service_hold}\n" for t in service_held))
        arrays = {"targets": ["primary"], "writers": WRITERS, "helpers": ["keycloakPostgreSQLInit"], "conditional": ["terrace-cms"],
                  "timers": timers, "held": held, "service_held": service_held}
        settings = {"restored": self.restored, "populated": self.populated, "adopted": self.adopted, "adopting": self.state / "adopting",
                    "replacement": self.dir / "replacement", "state_dir": self.state, "hold": self.hold, "service_hold": self.service_hold,
                    "hostname": "app", "stability_seconds": "15"}
        prelude = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in settings.items()) + "".join(f"{k}=({' '.join(shlex.quote(x) for x in v)})\n" for k, v in arrays.items())
        path = self.dir / "terrace-production"
        path.write_text(prelude + SCRIPT.read_text())
        self.timers = timers
        return path

    def run_tool(self, *args, timers=APP_TIMERS, held=BACKUP, service_held=("terrace-cms-sweep",), uid="0"):
        script = self.wrapper(list(timers), list(held), list(service_held))
        env = {"PATH": f"{self.bin}:{TOOLS_PATH}", "FAKE_UID": uid, "FAKE_UNITS": str(self.units), "FAKE_LOG": str(self.log),
               "FAKE_CONDITIONS": str(self.conditions), "FAKE_WRITERS": " ".join(WRITERS), "FAKE_TIMERS": " ".join(timers)}
        return subprocess.run([BASH, "-euo", "pipefail", str(script), *args], capture_output=True, text=True, timeout=30, env=env)

    def active(self, unit):
        path = self.units / unit
        return path.exists() and path.read_text().strip() == "active"

    def adopt_restored(self, **kwargs):
        self.restored.write_text("RECONCILIATION OK\n")
        self.populated.write_text("18\n")
        result = self.run_tool("adopt", "--restored", **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_adopt_restored_holds_both_groups_before_the_target_starts(self):
        out = self.adopt_restored().stdout.splitlines()
        self.assertEqual(out, ["terrace-secrets verify: PASS",
                               "Adopted: the writers start now and at every boot.",
                               "Held until you have verified the restored services: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer (they stay off across reboots).",
                               "Arm them with: terrace-production arm-timers",
                               "Held until the replacement is declared in service, after any erasure replay: terrace-cms-sweep.timer (they stay off across reboots).",
                               "Arm it then with: terrace-production arm-timers --in-service"])
        self.assertTrue(self.hold.exists() and self.service_hold.exists() and self.adopted.exists())
        # The target started with the markers in place: the held timers were skipped, the others armed.
        self.assertTrue(self.active("terrace-production.target") and self.active("terrace-identity-health.timer"))
        for timer in BACKUP + ["terrace-cms-sweep"]:
            self.assertFalse(self.active(timer + ".timer"), timer)
        # A later start of the target (as at boot) or of a held timer arms nothing held.
        self.run_tool("status")
        subprocess.run([str(self.bin / "systemctl"), "start", "terrace-production.target", "terrace-backup.timer"], check=True,
                       env={"PATH": f"{self.bin}:{TOOLS_PATH}", "FAKE_UNITS": str(self.units), "FAKE_LOG": str(self.log), "FAKE_CONDITIONS": str(self.conditions),
                            "FAKE_WRITERS": " ".join(WRITERS), "FAKE_TIMERS": " ".join(APP_TIMERS)})
        for timer in BACKUP + ["terrace-cms-sweep"]:
            self.assertFalse(self.active(timer + ".timer"), timer)

    def test_adopt_new_holds_nothing(self):
        result = self.run_tool("adopt", "--new", timers=APP_TIMERS)
        # A new host: the stub terrace-backup is absent, so the repository check refuses; give it one that says "not initialised".
        self.assertEqual(result.returncode, 1)
        (self.bin / "timeout").write_text(f"#!{BASH}\nexit 10\n")
        (self.bin / "timeout").chmod(0o700)
        (self.bin / "jq").write_text(f"#!{BASH}\nexit 99\n")
        (self.bin / "jq").chmod(0o700)
        result = self.run_tool("adopt", "--new")
        self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, "Adopted: the writers start now and at every boot."), result.stderr)
        self.assertFalse(self.hold.exists() or self.service_hold.exists())
        for timer in APP_TIMERS:
            self.assertTrue(self.active(timer + ".timer"), timer)
        result = self.run_tool("arm-timers", "--in-service")
        self.assertEqual((result.returncode, result.stdout), (0, "Already armed: nothing to arm.\nEvery writer timer is active now and at every boot.\n"))

    def test_the_two_stages_keep_their_order_and_are_idempotent(self):
        self.adopt_restored()
        # The sweep stage first: refused, both markers kept, nothing started.
        before = self.log.read_text()
        result = self.run_tool("arm-timers", "--in-service")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "terrace-production: the backup timers are still held (terrace-backup.timer terrace-backup-check.timer "
                                        "terrace-backup-freshness.timer); run terrace-production arm-timers first, after the first backup by hand "
                                        "(restore promotion step 21); nothing armed\n")
        self.assertTrue(self.hold.exists() and self.service_hold.exists())
        self.assertNotIn("start", self.log.read_text()[len(before):])
        # Stage one arms the backup timers and names the held sweep.
        still = "Still held until the replacement is declared in service: terrace-cms-sweep.timer. Arm with: terrace-production arm-timers --in-service\n"
        result = self.run_tool("arm-timers")
        self.assertEqual((result.returncode, result.stdout), (0, "Armed: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer.\n" + still))
        self.assertFalse(self.hold.exists())
        self.assertTrue(self.service_hold.exists())
        self.assertFalse(self.active("terrace-cms-sweep.timer"))
        self.assertEqual(self.run_tool("arm-timers").stdout, "Already armed: nothing to arm.\n" + still)
        status = self.run_tool("status").stdout
        self.assertNotIn("timers held after a restored adoption", status)
        self.assertIn("timers held until the replacement is declared in service: terrace-cms-sweep.timer (run terrace-production arm-timers --in-service after any erasure replay)", status)
        # Stage two arms the sweep.
        done = "Every writer timer is active now and at every boot.\n"
        result = self.run_tool("arm-timers", "--in-service")
        self.assertEqual((result.returncode, result.stdout), (0, "Armed: terrace-cms-sweep.timer.\n" + done))
        self.assertFalse(self.service_hold.exists())
        self.assertEqual(self.run_tool("arm-timers", "--in-service").stdout, "Already armed: nothing to arm.\n" + done)
        self.assertEqual(self.run_tool("arm-timers").stdout, "Already armed: nothing to arm.\n" + done)
        self.assertNotIn("timers held", self.run_tool("status").stdout)

    def test_status_names_each_held_group(self):
        self.adopt_restored()
        status = self.run_tool("status").stdout.splitlines()
        self.assertEqual(status[1:3], ["timers held after a restored adoption: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer "
                                       "(run terrace-production arm-timers once the restored services are verified)",
                                       "timers held until the replacement is declared in service: terrace-cms-sweep.timer "
                                       "(run terrace-production arm-timers --in-service after any erasure replay)"])

    def test_a_role_without_a_sweep(self):
        relay = {"timers": BACKUP, "held": BACKUP, "service_held": ()}
        self.adopt_restored(**relay)
        self.assertFalse(self.service_hold.exists())
        result = self.run_tool("arm-timers", **relay)
        self.assertEqual(result.stdout, "Armed: terrace-backup.timer terrace-backup-check.timer terrace-backup-freshness.timer.\n"
                                        "Every writer timer is active now and at every boot.\n")
        result = self.run_tool("arm-timers", "--in-service", **relay)
        self.assertEqual((result.returncode, result.stdout), (0, "Already armed: nothing to arm.\nThis role holds no timer until service (it has no sweep).\n"
                                                                 "Every writer timer is active now and at every boot.\n"))

    def test_refusals(self):
        self.assertEqual(self.run_tool("arm-timers", uid="1000").stderr, "terrace-production: run as root\n")
        result = self.run_tool("arm-timers")
        self.assertEqual((result.returncode, result.stderr), (1, "terrace-production: not adopted; finish terrace-production adopt first\n"))
        result = self.run_tool("arm-timers", "--everything")
        self.assertEqual((result.returncode, result.stderr), (2, "usage: terrace-production arm-timers [--in-service]\n"))
        self.adopt_restored()
        (self.units / "terrace-production.target").write_text("inactive\n")
        result = self.run_tool("arm-timers")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the writers are not running", result.stderr)
        self.assertTrue(self.hold.exists())


if __name__ == "__main__":
    unittest.main()
