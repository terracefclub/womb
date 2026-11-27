"""terrace-production's own logic (platform/production/terrace-production.sh), with a stub systemctl and tools.

platform/production/base.nix prepends the host's settings and builds the script with writeShellApplication (errexit,
nounset, pipefail); this test does the same with paths in a temporary directory. The stub systemctl keeps each unit's
state in a file and honours the held timers' ConditionPathExists=! the way systemd does (a start whose condition fails
leaves the timer inactive). The production VM proof runs the real script.
Run: python3 -B tests/platform/test_terrace_production.py -v
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(os.environ.get("TERRACE_PRODUCTION_SCRIPT", Path(__file__).resolve().parents[2] / "platform/production/terrace-production.sh"))
BASH = shutil.which("bash") or "/bin/bash"
TOOLS_PATH = ":".join(sorted({os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("bash", "cat", "rm", "install", "chmod", "date", "grep", "tr", "readlink", "jq", "sort", "comm", "tail", "head", "ln", "tee", "wc", "python3", "mv", "awk")}))
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
      # A unit's invocation: by default a new one after every switch (the switch ran it, so a failed unit listed after a
      # switch counts as one the switch broke); constant for a unit in $FAKE_DIR/untouched (failed before, left alone).
      "-p InvocationID --value")
        if [[ -e $FAKE_DIR/invocation-$unit ]]; then cat "$FAKE_DIR/invocation-$unit"
        elif grep -qsxF "$unit" "$FAKE_DIR/untouched"; then echo invocation-0
        else echo "invocation-$(cat "$FAKE_DIR/switch-count" 2>/dev/null || echo 0)"; fi ;;
      *) exit 99 ;;
    esac ;;
  is-failed) [[ $2 == --quiet && $(state "$3") == failed ]] ;;
  is-active)
    if [[ $2 == --quiet ]]; then [[ $(state "$3") == active ]]; else state "$2"; [[ $(state "$2") == active ]]; fi ;;
  reset-failed)
    echo "$*" >>"$FAKE_DIR/reset-failed.log"
    for unit in "${@:2}"; do
      if [[ $(state "$unit") == failed ]]; then echo inactive >"$FAKE_UNITS/$unit"; fi
      if [[ -e $FAKE_DIR/failed-units ]]; then { grep -v "^$unit loaded failed " "$FAKE_DIR/failed-units" || true; } >"$FAKE_DIR/failed-units.new"; mv "$FAKE_DIR/failed-units.new" "$FAKE_DIR/failed-units"; fi
    done ;;
  list-units)
    if [[ " $* " == *" terrace-cms-tool-* "* ]]; then
      [[ ! -e $FAKE_DIR/tools-fail ]] || exit 1
      cat "$FAKE_DIR/tools" 2>/dev/null || true
    else
      # Every unit after a switch: name load active sub description.
      cat "$FAKE_DIR/failed-units" 2>/dev/null || true
    fi ;;
  # Jobs still queued (terrace-production-boot starts the target with --no-block): $FAKE_DIR/jobs, if a test says.
  list-jobs)
    [[ ! -e $FAKE_DIR/jobs-fail ]] || exit 1
    cat "$FAKE_DIR/jobs" 2>/dev/null || true ;;
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


class Harness(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve()
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
            # No waiting; a test can make time pass by changing unit states at each sleep ($FAKE_DIR/on-sleep).
            "sleep": 'if [[ -e $FAKE_DIR/on-sleep ]]; then . "$FAKE_DIR/on-sleep"; fi\nexit 0\n',
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
        # The deploy path's paths (the host's /nix/store, system profile, /run/current-system and /run/booted-system).
        self.store = self.dir / "store"
        self.profiles = self.dir / "profiles"
        for path in (self.store, self.profiles):
            path.mkdir()
        self.profile = self.profiles / "system"
        self.current = self.dir / "current-system"
        self.booted = self.dir / "booted-system"
        self.marker = self.dir / "migrated-revision"
        self.deploy_keys = ["terrace-test-deploy-1:" + "A" * 43 + "="]

    def wrapper(self, timers, held, service_held):
        self.conditions.write_text("".join(f"{t}.timer={self.hold}\n" for t in held) + "".join(f"{t}.timer={self.service_hold}\n" for t in service_held))
        arrays = {"targets": ["primary"], "writers": WRITERS, "helpers": ["keycloakPostgreSQLInit"], "conditional": ["terrace-cms"],
                  "timers": timers, "held": held, "service_held": service_held, "deploy_keys": self.deploy_keys,
                  "busy": ["terrace-switch", "terrace-backup", "terrace-backup-check", "terrace-cms-migrate", "terrace-cms-sweep"], "quiesce": ["terrace-cms", "terrace-web"]}
        settings = {"restored": self.restored, "populated": self.populated, "adopted": self.adopted, "adopting": self.state / "adopting",
                    "replacement": self.dir / "replacement", "state_dir": self.state, "hold": self.hold, "service_hold": self.service_hold,
                    "hostname": "app", "stability_seconds": "15", "role": "app", "instance": "rehearsal", "cms_marker": self.marker,
                    "store_dir": self.store, "profile": self.profile, "current_system": self.current, "booted_system": self.booted}
        prelude = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in settings.items()) + "".join(f"{k}=({' '.join(shlex.quote(x) for x in v)})\n" for k, v in arrays.items())
        path = self.dir / "terrace-production"
        path.write_text(prelude + SCRIPT.read_text())
        self.timers = timers
        return path

    def run_tool(self, *args, timers=APP_TIMERS, held=BACKUP, service_held=("terrace-cms-sweep",), uid="0"):
        script = self.wrapper(list(timers), list(held), list(service_held))
        env = {"PATH": f"{self.bin}:{TOOLS_PATH}", "FAKE_UID": uid, "FAKE_DIR": str(self.dir), "BASH_BIN": BASH, "FAKE_UNITS": str(self.units), "FAKE_LOG": str(self.log),
               "FAKE_CONDITIONS": str(self.conditions), "FAKE_WRITERS": " ".join(WRITERS), "FAKE_TIMERS": " ".join(timers)}
        return subprocess.run([BASH, "-euo", "pipefail", str(script), *args], capture_output=True, text=True, timeout=30, env=env)

    def active(self, unit):
        path = self.units / unit
        return path.exists() and path.read_text().strip() == "active"


class TerraceProduction(Harness):
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


OLD_REV, NEW_REV = "1" * 40, "2" * 40
# switch-to-configuration's line naming the units it saw failed, exactly as nixpkgs' switch-to-configuration-ng prints it
# at the pinned nixpkgs (31ecbaca, pkgs/by-name/sw/switch-to-configuration-ng/src/main.rs:
# eprintln!("warning: the following units failed: {}", failed_units.join(", "))). The deploy VM proof finds the same
# text in the real switch binary.
SWITCH_FAILED_LINE = "warning: the following units failed: "
M_OLD = ["20261028_080047", "20261119_063012_link_indexes"]
M_NEW = M_OLD + ["20261119_165021_roster_journal"]
DEPLOY_STUBS = {
    # Valid unless listed in $FAKE_DIR/invalid; a closure is the path plus the lines of its `closure` file.
    "nix-store": r"""case $1 in
  --check-validity) if grep -qxF "$2" "$FAKE_DIR/invalid" 2>/dev/null; then exit 1; fi ;;
  -qR) p=$(readlink -f "$2"); echo "$p"; cat "$p/closure" 2>/dev/null || true ;;
  *) exit 99 ;;
esac
""",
    # `nix store verify`: a path listed in $FAKE_DIR/unsigned has no valid signature by a trusted key.
    "nix": r"""echo "$*" >>"$FAKE_DIR/nix.log"
rc=0
for a in "$@"; do if grep -qxF -- "$a" "$FAKE_DIR/unsigned" 2>/dev/null; then echo "path '$a' is untrusted" >&2; rc=2; fi; done
exit $rc
""",
    "nix-env": r"""profile=$2
case $3 in
  --set) n=0; for l in "$profile"-*-link; do [[ -L $l ]] || continue; k=${l%-link}; k=${k##*-}; (( k > n )) && n=$k; done; n=$((n + 1))
         ln -s "$4" "$profile-$n-link"; ln -sfn "${profile##*/}-$n-link" "$profile" ;;
  --switch-generation) [[ -L $profile-$4-link && ! -e $FAKE_DIR/switch-generation-fails ]] || exit 1; ln -sfn "${profile##*/}-$4-link" "$profile" ;;
  *) exit 99 ;;
esac
""",
    # Runs the unit's command in the foreground, as `--wait` would, and logs how it was asked.
    "flock": r"""exec python3 -c 'import fcntl, sys
try:
    fcntl.flock(int(sys.argv[2]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' "$@"
""",
    "systemd-run": r"""echo "$*" >>"$FAKE_DIR/systemd-run.log"
# A decision made against a state that changes before the unit runs: another activation got there first.
if [[ -e $FAKE_DIR/race ]]; then ln -sfn "$(cat "$FAKE_DIR/race")" "$FAKE_DIR/current-system"; fi
while [[ $1 != -- ]]; do shift; done
shift
# A unit stopped in the middle of its switch (`systemctl stop terrace-switch.service`): systemd counts the SIGTERM as a
# clean exit, so `systemd-run --wait` returns 0 (seen in the update drill). The switch stub sends the signal.
# systemd-run --wait not returning after its unit ended: the unit (active while it runs) ends, the waiter stays.
if [[ -e $FAKE_DIR/wait-hangs ]]; then
  echo active >"$FAKE_UNITS/terrace-switch.service"
  "$BASH_BIN" -euo pipefail "$@" || true
  command -p sleep 2
  echo inactive >"$FAKE_UNITS/terrace-switch.service"
  exec "$(command -p -v sleep)" 60
fi
if [[ -e $FAKE_DIR/stop-mid-switch ]]; then
  "$BASH_BIN" -euo pipefail "$@" &
  echo $! >"$FAKE_DIR/unit.pid"
  wait $! || true
  exit 0
fi
exec "$BASH_BIN" -euo pipefail "$@"
""",
    # psql as postgres: the CMS database exists while $FAKE_DIR/ledger does; its lines are the ledger.
    "runuser": r"""q=""
while (( $# )); do [[ $1 == -c ]] && q=$2; shift; done
case $q in
  *pg_database*) if [[ -e $FAKE_DIR/ledger ]]; then echo 1; else echo 0; fi ;;
  *to_regclass*) echo t ;;
  *payload_migrations*) cat "$FAKE_DIR/ledger" ;;
  *) exit 99 ;;
esac
""",
}


class DeployPath(Harness):
    """activate, rollback and the deploy part of status, against fake systems in a temporary store."""

    def setUp(self):
        super().setUp()
        for name, body in DEPLOY_STUBS.items():
            path = self.bin / name
            path.write_text(f"#!{BASH}\n" + body)
            path.chmod(0o700)
        self.old = self.system("old")
        (self.profiles / "system-1-link").symlink_to(self.old)
        self.profile.symlink_to("system-1-link")
        self.current.symlink_to(self.old)
        self.booted.symlink_to(self.old)
        self.new = self.system("new", website=NEW_REV, migrations=M_NEW, closure=[str(self.store / ("b" * 32 + "-cms-new")), str(self.store / ("c" * 32 + "-glibc"))])
        # glibc is shared with the running system; only the CMS and the toplevel are new.
        (self.old / "closure").write_text(str(self.store / ("c" * 32 + "-glibc")) + "\n")
        self.set_unit("postgresql.service", "active")
        self.marker.write_text(OLD_REV + "\n")
        self.ledger(M_OLD)

    def system(self, name, role="app", host="app", instance="rehearsal", website=OLD_REV, migrations=M_OLD, keycloak="26.4.1", postgresql="18.1", kernel="1",
               closure=(), switch_rc=0, manifest=True, switchable=True, reported="", reported_line=None):
        path = self.store / (hashlib.sha1(name.encode()).hexdigest()[:32] + "-nixos-system-" + name)
        (path / "etc" / "terrace").mkdir(parents=True)
        for item in ("kernel", "initrd", "kernel-modules"):
            target = self.store / f"{item}-{kernel}"
            target.touch()
            (path / item).symlink_to(target)
        if switchable:
            (path / "bin").mkdir()
            (path / "bin" / "switch-to-configuration").write_text(
                f"#!{BASH}\necho \"{name} $1\" >>\"$FAKE_DIR/switch.log\"\n"
                + "echo $(( $(cat \"$FAKE_DIR/switch-count\" 2>/dev/null || echo 0) + 1 )) >\"$FAKE_DIR/switch-count\"\n"
                + "if [[ -e $FAKE_DIR/stop-mid-switch ]]; then kill -TERM \"$(cat \"$FAKE_DIR/unit.pid\")\"; sleep 1; fi\n"
                # The gated CMS and website the switch starts (through terrace-production.target): refused by their gate (failed)
                # when the marker exists and is not this system's revision; otherwise left as they were (a running unit is
                # not modelled). Off with $FAKE_DIR/no-gate-emulation, for tests that set unit states themselves.
                + (f"if [[ $1 == switch && ! -e $FAKE_DIR/no-gate-emulation && -e {shlex.quote(str(self.marker))} && $(cat {shlex.quote(str(self.marker))}) != {website or ''} ]]; then\n"
                   "  for u in terrace-cms.service terrace-web.service; do\n"
                   "    echo failed >\"$FAKE_UNITS/$u\"\n"
                   "    { grep -v \"^$u \" \"$FAKE_DIR/failed-units\" 2>/dev/null || true; echo \"$u loaded failed failed gated\"; } >\"$FAKE_DIR/failed-units.new\"\n"
                   "    mv \"$FAKE_DIR/failed-units.new\" \"$FAKE_DIR/failed-units\"\n"
                   "  done\n"
                   "fi\n")
                # What else the switch does to units, if a test says.
                + "if [[ -e $FAKE_DIR/on-switch ]]; then . \"$FAKE_DIR/on-switch\"; fi\n"
                + (f"echo '{SWITCH_FAILED_LINE if reported_line is None else reported_line}{reported}'\n" if reported else "")
                + ""
                # Exit 4 is "switched, some units failed"; any other failure models the boot loader step, before the switch.
                f"if [[ $1 == switch && ( {switch_rc} == 0 || {switch_rc} == 4 ) ]]; then ln -sfn {shlex.quote(str(path))} \"$FAKE_DIR/current-system\"; fi\nexit {switch_rc}\n")
            (path / "bin" / "switch-to-configuration").chmod(0o700)
        if manifest:
            (path / "etc" / "terrace" / "system.json").write_text(json.dumps({
                "schema": 1, "role": role, "hostName": host, "instance": instance, "wombRevision": hashlib.sha1(name.encode()).hexdigest(),
                "website": {"revision": website, "migrations": migrations}, "versions": {"keycloak": keycloak, "postgresql": postgresql, "headscale": None}}))
        (path / "closure").write_text("".join(line + "\n" for line in closure))
        return path

    def set_unit(self, unit, state):
        (self.units / unit).write_text(state + "\n")

    def ledger(self, names):
        (self.dir / "ledger").write_text("".join(name + "\n" for name in names))

    def generation(self):
        return os.readlink(self.profile)

    def records(self):
        path = self.state / "deployments.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def accepted(self, *args, **kwargs):
        result = self.run_tool(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def refused(self, needle, *args, **kwargs):
        before = (self.generation(), os.readlink(self.current), len(self.records()))
        result = self.run_tool(*args, **kwargs)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn(needle, result.stderr)
        self.assertEqual((self.generation(), os.readlink(self.current), len(self.records())), before, "a refusal changed something")
        self.assertFalse((self.dir / "systemd-run.log").exists(), "a refusal started the switch unit")
        return result

    def test_activate_then_rollback_through_the_switch_unit(self):
        out = self.accepted("activate", str(self.new)).stdout
        self.assertIn("Signatures: 2 new path(s) verified against 1 trusted deploy key(s).", out)
        verify = (self.dir / "nix.log").read_text().split()
        self.assertEqual(verify[:7], ["--extra-experimental-features", "nix-command", "store", "verify", "--sigs-needed", "1", "--option"])
        self.assertEqual(verify[7:9], ["trusted-public-keys", self.deploy_keys[0]])
        self.assertEqual(sorted(verify[9:]), sorted([str(self.new), str(self.store / ("b" * 32 + "-cms-new"))]), "only paths outside every generation are checked")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-2-link", str(self.new)))
        self.assertEqual((self.dir / "switch.log").read_text(), "new switch\n")
        self.assertIn("--unit=terrace-switch --collect --service-type=exec --wait", (self.dir / "systemd-run.log").read_text())
        record = self.records()[-1]
        self.assertEqual({k: record[k] for k in ("action", "mode", "from", "to", "generationBefore", "generationAfter", "websiteRevision", "result")},
                         {"action": "activate", "mode": "switch", "from": str(self.old), "to": str(self.new), "generationBefore": "1", "generationAfter": "2",
                          "websiteRevision": NEW_REV, "result": "switched; down until the CMS migration: terrace-cms.service terrace-web.service"})
        self.assertIn("THE SITE IS DOWN until the CMS migration", out)
        status = self.accepted("status").stdout
        self.assertIn(f"running system: {self.new} (Womb {hashlib.sha1(b'new').hexdigest()}, website {NEW_REV})", status)
        self.assertIn("generation: 2 (the boot default)\n", status)
        self.assertNotIn("WARNING", status)
        self.assertIn(f"previous generation: 1 {self.old} (Womb {hashlib.sha1(b'old').hexdigest()}, website {OLD_REV})", status)
        self.assertIn("deploy path: on, 1 trusted deploy key(s)", status)
        self.assertIn('"action":"activate"', status)
        # Before any migration (case A) the way back is allowed; the switch runs in the same unit.
        (self.dir / "systemd-run.log").unlink()
        self.accepted("rollback")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))
        self.assertEqual(self.records()[-1]["action"], "rollback")
        self.assertIn("--unit=terrace-switch", (self.dir / "systemd-run.log").read_text())
        self.assertEqual(self.records()[-1]["generationAfter"], "1")
        # The older generation is the newest one below the current: nothing older than generation 1.
        (self.dir / "systemd-run.log").unlink()
        self.refused("no generation older than the current one (1)", "rollback")

    def test_identity_and_form_of_the_target(self):
        self.refused("terrace-production: run as root", "activate", str(self.new), uid="1000")
        for needle, path in [("is not a store path", "relative/path"), ("is not a store path", str(self.new) + "/bin"),
                             ("is not a store path", str(self.dir / ("d" * 32 + "-x"))), ("does not exist on this host", str(self.store / ("e" * 32 + "-absent")))]:
            self.refused(needle, "activate", path)
        (self.dir / "invalid").write_text(str(self.new) + "\n")
        self.refused("is not a valid path in this host's Nix store", "activate", str(self.new))
        (self.dir / "invalid").unlink()
        self.refused("is not a NixOS system", "activate", str(self.system("noswitch", switchable=False)))
        self.refused("has no Terrace system manifest", "activate", str(self.system("nomanifest", manifest=False)))
        for kwargs, got in [({"role": "relay"}, "'1 relay app rehearsal'"), ({"host": "other"}, "'1 app other rehearsal'"), ({"instance": "production"}, "'1 app app production'")]:
            result = self.refused("was built for (schema role host instance)", "activate", str(self.system("wrong-" + "-".join(kwargs.values()), **kwargs)))
            self.assertIn(got + "; this host is '1 app app rehearsal'", result.stderr)
        self.refused("is already the running system", "activate", str(self.old))
        # Control: the same checks accept this host's own new system.
        self.accepted("activate", str(self.new))

    def test_deploy_path_off_without_a_trusted_key(self):
        self.deploy_keys = []
        self.refused("no deploy key is trusted on this host", "activate", str(self.new))
        self.assertIn("deploy path: off", self.accepted("status").stdout)
        self.deploy_keys = ["terrace-test-deploy-1:" + "A" * 43 + "="]
        self.accepted("activate", str(self.new))

    def test_unsigned_paths_are_refused(self):
        (self.dir / "unsigned").write_text(str(self.store / ("b" * 32 + "-cms-new")) + "\n")
        result = self.refused("not all carry a valid signature by a trusted deploy key", "activate", str(self.new))
        self.assertIn("-cms-new' is untrusted", result.stderr)
        (self.dir / "unsigned").unlink()
        self.accepted("activate", str(self.new))

    def test_a_running_switch_migration_or_backup_blocks(self):
        for unit in ["terrace-switch", "terrace-cms-migrate", "terrace-backup"]:
            self.set_unit(unit + ".service", "active")
            self.refused(f"{unit}.service is active; wait until it has finished", "activate", str(self.new))
            self.set_unit(unit + ".service", "inactive")
        self.accepted("activate", str(self.new))

    def test_a_kernel_change_needs_boot(self):
        kernel = self.system("kernel", website=NEW_REV, migrations=M_NEW, kernel="2")
        self.refused("kernel initrd kernel-modules differ(s) from the booted system; repeat with --boot", "activate", str(kernel))
        out = self.accepted("activate", str(kernel), "--boot").stdout
        self.assertIn("Generation 2 is the boot default.\nReboot to run it, or cancel it with terrace-production rollback.", out)
        self.assertEqual((self.dir / "switch.log").read_text(), "kernel boot\n")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-2-link", str(self.old)))
        self.assertIn("is not the running system: a reboot boots generation 2. Expected after activate --boot", self.accepted("status").stdout)

    def test_upstream_versions(self):
        keycloak = self.system("keycloak", website=NEW_REV, migrations=M_NEW, keycloak="26.5.0")
        self.refused("upstream version change: keycloak 26.4.1 -> 26.5.0", "activate", str(keycloak))
        postgresql = self.system("postgresql", website=NEW_REV, migrations=M_NEW, postgresql="19.0")
        self.refused("PostgreSQL major version changes (18.1 -> 19.0)", "activate", str(postgresql), "--accept-upstream-schema-change")
        minor = self.system("minor", website=NEW_REV, migrations=M_NEW, postgresql="18.2")
        self.accepted("activate", str(minor))
        result = self.accepted("activate", str(keycloak), "--accept-upstream-schema-change")
        self.assertIn("WARNING: upstream version change accepted (U3)", result.stderr)

    def test_a_new_release_needs_the_cms_quiesced(self):
        for unit in ["terrace-cms", "terrace-web"]:
            self.set_unit(unit + ".service", "active")
            self.refused(f"the release changes and {unit}.service is active; quiesce first", "activate", str(self.new))
            self.set_unit(unit + ".service", "inactive")
        # The same release (a Nix-only change) needs no quiesce.
        self.set_unit("terrace-cms.service", "active")
        self.accepted("activate", str(self.system("same-release")))

    def test_the_ledger_needs_postgresql_when_a_marker_exists(self):
        self.set_unit("postgresql.service", "inactive")
        self.refused("PostgreSQL is not running, so the CMS migration ledger cannot be compared", "activate", str(self.new))
        self.marker.unlink()
        self.accepted("activate", str(self.new))

    def test_forward_only_migrations(self):
        self.accepted("activate", str(self.new))
        # The new release migrated: ledger and marker say so.
        self.ledger(M_NEW)
        self.marker.write_text(NEW_REV + "\n")
        (self.dir / "systemd-run.log").unlink()
        result = self.refused("the CMS database has migrations the target release does not have (20261119_165021_roster_journal)", "rollback")
        self.assertIn("--cms-stays-down", result.stderr)
        result = self.accepted("rollback", "--cms-stays-down")
        self.assertIn("WARNING: the CMS stays down", result.stderr)
        self.assertEqual(os.readlink(self.current), str(self.old))
        # Back to the new release: the ledger is a subset of its list, so no flag.
        self.accepted("activate", str(self.new))

    def test_a_failed_migration_never_rolls_back_onto_a_newer_schema(self):
        self.accepted("activate", str(self.new))
        # bin/migrate recorded the new migration but failed: the marker keeps the OLD revision (H9).
        self.ledger(M_NEW)
        (self.dir / "systemd-run.log").unlink()
        result = self.refused("a failed or partial migration, H9", "rollback", "--cms-stays-down")
        self.assertIn("Not allowed by any flag", result.stderr)
        # An unknown migration list on the target refuses any ledger.
        unknown = self.system("unknown", migrations=None)
        self.marker.write_text(NEW_REV + "\n")
        self.refused("records no CMS migration list", "activate", str(unknown), "--cms-stays-down")

    def test_a_failed_switch_is_recorded(self):
        broken = self.system("broken", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        result = self.run_tool("activate", str(broken))
        self.assertEqual(result.returncode, 1)
        self.assertIn("activate FAILED (exit 1)", result.stderr)
        record = self.records()[-1]
        self.assertEqual(record["result"], "failed (exit 1)")
        # The generation was registered before the switch (as nixos-rebuild does), and the switch failed before the new
        # system ran: the command made the running system the boot default again, in the same unit, and recorded it.
        self.assertEqual((record["generationAfter"], record["revert"], record["bootDefaultAfter"]), ("2", {"from": "2", "to": "1", "result": "success"}, "1"))
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))
        self.assertTrue((self.profiles / "system-2-link").is_symlink(), "the failed generation stays in the list")
        self.assertEqual((self.dir / "switch.log").read_text(), "broken switch\nold boot\n")
        self.assertIn("the boot default was set back to generation 1, the running system; generation 2 stays in the list, recorded as failed", result.stderr)
        status = self.accepted("status").stdout
        self.assertNotIn("WARNING", status)
        self.assertIn('"result":"failed (exit 1)"', status)
        # Never in boot mode: a pending boot default is what --boot asks for, also when its boot step fails.
        boot = self.system("broken-boot", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        result = self.run_tool("activate", str(boot), "--boot")
        self.assertEqual((result.returncode, self.records()[-1]["revert"], self.generation()), (1, None, "system-3-link"))
        self.assertIn(f"WARNING: the boot default, generation 3 ({boot}), is not the running system", self.accepted("status").stdout)

    def test_a_revert_that_fails_is_recorded_and_left_to_the_operator(self):
        broken = self.system("broken", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        # The running system's own boot step fails too (the boot loader cause is still there).
        (self.dir / "on-switch").write_text("if [[ $1 == boot ]]; then exit 7; fi\n")
        result = self.run_tool("activate", str(broken))
        record = self.records()[-1]
        self.assertEqual((result.returncode, record["result"], record["revert"]), (1, "failed (exit 1)", {"from": "2", "to": "1", "result": "failed (exit 7)"}))
        self.assertIn("WARNING: setting the boot default back to the running system did not work (failed (exit 7))", result.stderr)
        self.assertIn("the boot menu may still default to generation 2", result.stderr)
        self.assertEqual((self.dir / "switch.log").read_text(), "broken switch\nold boot\n", "one attempt, no loop")
        self.assertIn("WARNING: the last switch failed and setting the boot default back to the running system did not work", self.accepted("status").stdout)
        # The profile step failing instead: the boot default stays the failed generation; once the cause is fixed, the
        # manual fallback (rollback, which cancels a pending boot default) works.
        (self.dir / "on-switch").unlink()
        (self.dir / "switch-generation-fails").touch()
        again = self.system("broken-again", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        result = self.run_tool("activate", str(again))
        self.assertEqual((result.returncode, self.records()[-1]["revert"]["result"], self.generation()), (1, "failed (exit 1)", "system-3-link"))
        self.assertIn("the boot default is still generation 3, which a reboot would boot", result.stderr)
        status = self.accepted("status").stdout
        self.assertIn("WARNING: the boot default, generation 3", status)
        (self.dir / "switch-generation-fails").unlink()
        self.assertIn("the boot default goes back to generation 1", self.accepted("rollback").stdout)
        self.assertEqual(self.generation(), "system-1-link")

    def test_a_watched_wait_that_does_not_return(self):
        # systemd-run --wait not returning after its unit ended (seen once in the update drill): the client stops waiting
        # after switch_gone_seconds and the records decide.
        (self.dir / "wait-hangs").touch()
        result = self.run_tool("activate", str(self.new))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the terrace-switch unit has ended, but systemd-run --wait had not returned 10 s later; stopped waiting for it, the records decide", result.stderr)
        self.assertEqual((self.records()[-1]["phase"], self.generation()), ("finished", "system-2-link"))

    def test_a_release_switch_with_the_gate_closed(self):
        # switch-to-configuration exits 4 when units failed: here the CMS and the website, both refused by their migration
        # gate for a release not yet migrated.
        gated = self.system("gated", website=NEW_REV, migrations=M_NEW, switch_rc=4)
        both = "terrace-cms.service loaded failed failed CMS\nterrace-web.service loaded failed failed web\n"
        (self.dir / "failed-units").write_text(both)
        out = self.accepted("activate", str(gated)).stdout
        self.assertIn("THE SITE IS DOWN until the CMS migration: the gate is closed for this release, so terrace-cms.service terrace-web.service stay failed until it", out)
        record = self.records()[-1]
        self.assertEqual((record["result"], record["failedUnits"], record["unexpectedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", ["terrace-cms.service", "terrace-web.service"], []))
        self.assertNotIn("startingUnits", record)
        self.assertIn("reset-failed terrace-cms.service terrace-web.service", (self.dir / "reset-failed.log").read_text())
        # The units are still retrying their gate (Restart=on-failure) when the switch returns: the record waits until
        # they have stopped at their start limit, and says so.
        self.accepted("activate", str(self.old))
        retrying = (f"echo activating >{self.units}/terrace-web.service\n"
                    f"printf 'terrace-cms.service loaded failed failed CMS\\nterrace-web.service loaded activating auto-restart web\\n' >{self.dir}/failed-units\n")
        (self.dir / "on-switch").write_text(retrying)
        (self.dir / "on-sleep").write_text(f"echo failed >{self.units}/terrace-web.service; printf %s {shlex.quote(both)} >{self.dir}/failed-units\n")
        self.accepted("activate", str(gated))
        record = self.records()[-1]
        self.assertEqual((record["result"], record["failedUnits"], record["unexpectedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", ["terrace-cms.service", "terrace-web.service"], []))
        # One that is still starting after the wait (300 s) is not the gate's refusal: a failed switch, naming it.
        (self.dir / "on-switch").unlink()
        self.accepted("activate", str(self.old))
        (self.dir / "on-switch").write_text(retrying)
        (self.dir / "on-sleep").write_text(f"echo 2 >>{self.dir}/slept\n")
        result = self.run_tool("activate", str(gated))
        record = self.records()[-1]
        self.assertEqual((result.returncode, record["result"], record["failedUnits"], record["unexpectedUnits"]),
                         (1, "failed (exit 4, then the wait for the CMS and website timed out after 300 s, still starting: terrace-web.service)", ["terrace-cms.service"], ["terrace-web.service"]))
        self.assertEqual((record["waitTimedOut"], record["stillStarting"]), (True, ["terrace-web.service"]))
        self.assertIn("result: failed (exit 4, then the wait for the CMS and website timed out after 300 s, still starting: terrace-web.service)", result.stderr)
        self.assertEqual(len((self.dir / "slept").read_text().split()), 150, "waited 150 times 2 s, then gave up")

    def test_a_gated_switch_that_exits_0(self):
        # The gated units' starts are not the switch's own jobs (terrace-production-boot starts the target again), and when
        # it lists the units they are in their restart delay with no failed main process: it exits 0 (deploy VM proof).
        gated = self.system("gated0", website=NEW_REV, migrations=M_NEW, switch_rc=0)
        both = "terrace-cms.service loaded failed failed CMS\nterrace-web.service loaded failed failed web\n"
        (self.dir / "on-switch").write_text(f"echo activating >{self.units}/terrace-cms.service; echo activating >{self.units}/terrace-web.service\n")
        (self.dir / "on-sleep").write_text(f"echo failed >{self.units}/terrace-cms.service; echo failed >{self.units}/terrace-web.service; printf %s {shlex.quote(both)} >{self.dir}/failed-units\n")
        out = self.accepted("activate", str(gated)).stdout
        self.assertIn("THE SITE IS DOWN until the CMS migration", out)
        record = self.records()[-1]
        self.assertEqual(record["switchExit"], 0)
        self.assertEqual((record["result"], record["failedUnits"], record["unexpectedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", ["terrace-cms.service", "terrace-web.service"], []))
        # The same with another unit failing too: a failed switch, though the switch itself exited 0.
        (self.dir / "on-switch").unlink()
        (self.dir / "on-sleep").unlink()
        (self.dir / "failed-units").write_text("")
        self.accepted("rollback")
        (self.dir / "on-switch").write_text(f"echo failed >{self.units}/terrace-cms.service; echo failed >{self.units}/terrace-web.service\n"
                                            f"printf %s {shlex.quote(both + 'nginx.service loaded failed failed nginx' + chr(10))} >{self.dir}/failed-units\n")
        again = self.system("gated0-again", website=NEW_REV, migrations=M_NEW, switch_rc=0)
        result = self.run_tool("activate", str(again))
        record = self.records()[-1]
        self.assertEqual((result.returncode, record["result"], record["unexpectedUnits"]),
                         (1, "failed (exit 0, then units failed: terrace-cms.service terrace-web.service nginx.service)", ["nginx.service"]))
        self.assertIn("activate FAILED", result.stderr)
        # And the CMS failing where its gate is open (the marker holds the target's revision) after exit 0: failed too.
        (self.dir / "on-switch").unlink()
        (self.dir / "failed-units").write_text("")
        for unit in ("terrace-cms.service", "terrace-web.service"):
            self.set_unit(unit, "inactive")
        self.accepted("activate", str(self.old))
        self.marker.write_text(NEW_REV + "\n")
        (self.dir / "on-switch").write_text(f"echo failed >{self.units}/terrace-cms.service; echo failed >{self.units}/terrace-web.service; printf %s {shlex.quote(both)} >{self.dir}/failed-units\n")
        third = self.system("gated0-open", website=NEW_REV, migrations=M_NEW, switch_rc=0)
        result = self.run_tool("activate", str(third))
        self.assertEqual((result.returncode, self.records()[-1]["result"]), (1, "failed (exit 0, then units failed: terrace-cms.service terrace-web.service)"))

    def test_other_failed_units_fail_the_switch(self):
        gated = self.system("gated", website=NEW_REV, migrations=M_NEW, switch_rc=4)
        # Another unit failed too: a failed switch, whatever the CMS does.
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\nnginx.service loaded failed failed nginx\n")
        result = self.run_tool("activate", str(gated))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records()[-1]["result"], "failed (exit 4)")
        self.assertEqual((self.records()[-1]["failedUnits"], self.records()[-1]["unexpectedUnits"]), (["terrace-cms.service", "terrace-web.service"], ["nginx.service"]))
        # The CMS failing where its gate is OPEN (the marker holds the target's revision) is a failed switch too.
        self.marker.write_text(NEW_REV + "\n")
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\n")
        (self.dir / "systemd-run.log").unlink()
        self.accepted("rollback")
        result = self.run_tool("activate", str(gated))
        self.assertEqual((result.returncode, self.records()[-1]["result"]), (1, "failed (exit 4)"))
        # Exit 4 with no failed unit listed is not explained either (here the gated units were not even started).
        (self.dir / "failed-units").unlink()
        self.marker.write_text(OLD_REV + "\n")
        self.accepted("activate", str(self.old))
        (self.dir / "no-gate-emulation").touch()
        result = self.run_tool("activate", str(gated))
        self.assertEqual((result.returncode, self.records()[-1]["result"]),
                         (1, "failed (exit 4, then not started though the gate is closed for this release, THE SITE IS DOWN: terrace-cms.service terrace-web.service)"))

    def test_a_failed_non_service_or_restarting_unit_fails_a_gated_switch(self):
        cms = "terrace-cms.service loaded failed failed CMS\n"
        cases = [("terrace-backup.timer loaded failed failed timer\n", "", ["terrace-backup.timer"]),
                 ("keycloak.service loaded activating auto-restart Keycloak\n", "", ["keycloak.service"]),
                 ("", "terrace-cms.service, nginx.service", ["nginx.service"])]
        for index, (extra, reported, unexpected) in enumerate(cases):
            gated = self.system(f"gated-{index}", website=NEW_REV, migrations=M_NEW, switch_rc=4, reported=reported)
            (self.dir / "failed-units").write_text(cms + extra)
            result = self.run_tool("activate", str(gated))
            record = self.records()[-1]
            self.assertEqual((result.returncode, record["result"], record["unexpectedUnits"]), (1, "failed (exit 4)", unexpected), (extra, reported))
            self.assertIn("activate FAILED (exit 4)", result.stderr)
            (self.dir / "failed-units").write_text("")
            self.accepted("activate", str(self.old))
        # Control: a unit merely activating (a timer's job) is not this switch's failure; the switch's own list naming only the CMS is fine.
        gated = self.system("gated-control", website=NEW_REV, migrations=M_NEW, switch_rc=4, reported="terrace-cms.service")
        (self.dir / "failed-units").write_text(cms + "terrace-identity-health.service loaded activating start health\n")
        self.accepted("activate", str(gated))
        self.assertEqual((self.records()[-1]["result"], self.records()[-1]["unexpectedUnits"]), ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", []))

    def test_a_failed_migration_unit_is_reset_before_a_switch(self):
        # Left failed by a failed or timed-out migration: recorded in failedBefore and reset, so the corrected build's
        # activation (fixing forward) is not judged by it.
        self.set_unit("terrace-cms-migrate.service", "failed")
        (self.dir / "failed-units").write_text("terrace-cms-migrate.service loaded failed failed migration\n")
        corrected = self.system("corrected", website=NEW_REV, migrations=M_NEW, switch_rc=4)
        self.accepted("activate", str(corrected))
        started, finished = self.records()[-2:]
        self.assertEqual(started["failedBefore"], ["terrace-cms-migrate.service"])
        self.assertEqual((finished["result"], finished["unexpectedUnits"], finished["preexistingFailedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", [], []))
        self.assertIn("terrace-cms-migrate.service", (self.dir / "reset-failed.log").read_text())

    def test_units_failed_before_the_switch_and_left_alone_are_a_warning(self):
        # nginx failed before the switch; the switch did not run it again (same invocation): exit 4 is explained by it.
        (self.dir / "failed-units").write_text("nginx.service loaded failed failed nginx\n")
        (self.dir / "untouched").write_text("nginx.service\n")
        same = self.system("same", switch_rc=4, reported="nginx.service")
        result = self.accepted("activate", str(same))
        record = self.records()[-1]
        self.assertEqual((record["result"], record["switchExit"], record["unexpectedUnits"], record["preexistingFailedUnits"]), ("success", 4, [], ["nginx.service"]))
        self.assertIn("WARNING: already failed before this switch and left untouched by it, so not counted against it: nginx.service", result.stderr)
        # With the gate closed too: switched with the CMS and website down, nginx a warning.
        gated = self.system("gated", website=NEW_REV, migrations=M_NEW, switch_rc=4, reported="terrace-cms.service, nginx.service")
        self.accepted("activate", str(gated))
        self.assertEqual((self.records()[-1]["result"], self.records()[-1]["preexistingFailedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", ["nginx.service"]))
        self.accepted("activate", str(self.old))
        # Control: the same unit run again by the switch (a new invocation) and failed again: a failed switch.
        (self.dir / "untouched").unlink()
        again = self.system("again", switch_rc=4, reported="nginx.service")
        result = self.run_tool("activate", str(again))
        self.assertEqual((result.returncode, self.records()[-1]["result"], self.records()[-1]["unexpectedUnits"], self.records()[-1]["preexistingFailedUnits"]),
                         (1, "failed (exit 4)", ["nginx.service"], []))
        # Exit 4 with nothing failed at all is still not explained.
        (self.dir / "failed-units").write_text("")
        self.accepted("activate", str(self.old))
        bare = self.system("bare", switch_rc=4)
        self.assertEqual((self.run_tool("activate", str(bare)).returncode, self.records()[-1]["result"]), (1, "failed (exit 4)"))

    def test_one_command_at_a_time(self):
        lock = self.state / "deploy.lock"
        lock.touch(mode=0o600)
        with open(lock) as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.refused("another terrace-production activate or rollback is running on this host", "activate", str(self.new))
        self.accepted("activate", str(self.new))

    def test_a_decision_on_a_changed_state_is_not_applied(self):
        # An earlier run left records; between the checks and the unit, another activation switched the host: the unit
        # refuses and records nothing, and none of the earlier records is printed as this run's.
        self.accepted("activate", str(self.new))
        (self.dir / "switch.log").unlink()
        earlier = self.records()
        third = self.system("third", website=NEW_REV, migrations=M_NEW)
        other = self.system("other-activation", website=NEW_REV, migrations=M_NEW)
        (self.dir / "race").write_text(str(other))
        result = self.run_tool("activate", str(third))
        self.assertEqual(result.returncode, 1)
        self.assertIn("the running system or the profile changed after the checks", result.stderr)
        self.assertEqual((self.generation(), self.records(), (self.dir / "switch.log").exists()), ("system-2-link", earlier, False))
        self.assertNotIn('"action"', result.stdout, "no record of another run is printed as this one's")
        (self.dir / "race").unlink()
        self.current.unlink()
        self.current.symlink_to(self.new)
        self.accepted("activate", str(third))

    def test_rollback_cancels_a_pending_boot_default(self):
        kernel = self.system("kernel", website=NEW_REV, migrations=M_NEW, kernel="2")
        self.accepted("activate", str(kernel), "--boot")
        self.assertIn("WARNING: the boot default", self.accepted("status").stdout)
        out = self.accepted("rollback").stdout
        self.assertIn(f"the boot default goes back to generation 1, the running system {self.old}", out)
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))
        self.assertEqual((self.dir / "switch.log").read_text(), "kernel boot\nold boot\n")
        self.assertEqual((self.records()[-1]["action"], self.records()[-1]["mode"], self.records()[-1]["result"]), ("cancel", "boot", "success"))
        self.assertNotIn("WARNING", self.accepted("status").stdout)
        # The same after a switch that failed after registering its generation.
        # After a switch that failed after registering its generation, the command itself set the boot default back.
        broken = self.system("broken", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        self.assertEqual(self.run_tool("activate", str(broken)).returncode, 1)
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))
        self.assertNotIn("WARNING", self.accepted("status").stdout)

    def test_started_and_finished_records(self):
        self.set_unit("terrace-cms.service", "failed")
        self.accepted("activate", str(self.system("same-release")))
        started, finished = self.records()[-2:]
        self.assertEqual((started["phase"], started["failedBefore"], finished["phase"]), ("started", ["terrace-cms.service"], "finished"))
        # A unit killed mid-switch (or a reboot) leaves only the started record: status says so.
        with open(self.state / "deployments.log", "a") as log:
            log.write(json.dumps({"phase": "started", "time": "2026-11-24T00:00:00Z", "action": "activate"}) + "\n")
        self.assertIn("WARNING: the last activation started at 2026-11-24T00:00:00Z and never finished", self.accepted("status").stdout)

    def test_sweep_tools_and_unlistable_units_block(self):
        self.set_unit("terrace-cms-sweep.service", "active")
        self.refused("terrace-cms-sweep.service is active; wait until it has finished", "activate", str(self.new))
        self.set_unit("terrace-cms-sweep.service", "inactive")
        (self.dir / "tools").write_text("terrace-cms-tool-roster-invite-1.service loaded active running tool\n")
        self.refused("an operator tool runs (terrace-cms-tool-roster-invite-1.service", "activate", str(self.new))
        (self.dir / "tools").unlink()
        (self.dir / "tools-fail").touch()
        self.refused("cannot list the terrace-cms-tool units", "activate", str(self.new))
        (self.dir / "tools-fail").unlink()
        self.accepted("activate", str(self.new))

    def test_quiesce_counts_a_restarting_unit_and_postgresql_versions_must_exist(self):
        self.set_unit("terrace-web.service", "activating")
        self.refused("the release changes and terrace-web.service is activating; quiesce first", "activate", str(self.new))
        self.set_unit("terrace-web.service", "inactive")
        self.refused("a manifest records no PostgreSQL version", "activate", str(self.system("no-postgresql", website=NEW_REV, migrations=M_NEW, postgresql=None)))
        self.accepted("activate", str(self.new))

    def test_dry_run_changes_nothing(self):
        out = self.run_tool("activate", str(self.new), "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Checks passed; activate", out.stdout)
        self.assertIn("Dry run: nothing changed.", out.stdout)
        self.assertEqual((self.generation(), os.readlink(self.current), self.records(), (self.dir / "systemd-run.log").exists()), ("system-1-link", str(self.old), [], False))
        # The lock file the command created is root-only.
        self.assertEqual(oct((self.state / "deploy.lock").stat().st_mode & 0o777), "0o600")

    def test_rollback_skips_a_failed_attempts_generation(self):
        # activate X fails after registering generation 2 (the boot default is set back at once); activate Y makes generation 3.
        broken = self.system("broken", website=NEW_REV, migrations=M_NEW, switch_rc=1)
        self.assertEqual(self.run_tool("activate", str(broken)).returncode, 1)
        self.assertEqual(self.generation(), "system-1-link")
        self.accepted("activate", str(self.new))
        self.assertEqual(self.generation(), "system-3-link")
        status = self.accepted("status").stdout
        self.assertIn(f"previous generation: 1 {self.old} (", status)
        self.assertIn("skipped by rollback, its last record failed: generation 2 ('failed (exit 1)')", status)
        # A plain rollback passes over generation 2 and says so; generation 1 (its last record a success) is the target.
        out = self.accepted("rollback", "--dry-run").stdout
        self.assertIn("Skipping generation 2: its last record is 'failed (exit 1)' (rollback --generation 2 chooses it anyway).", out)
        self.assertIn(f"Checks passed; rollback {self.old} (", out)
        # An explicit choice of the failed generation: every check, and a warning.
        result = self.accepted("rollback", "--generation", "2", "--dry-run")
        self.assertIn(f"Checks passed; rollback {broken} (", result.stdout)
        self.assertIn("WARNING: generation 2 was chosen explicitly; its last record: failed (exit 1)", result.stderr)
        self.accepted("rollback")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))

    def test_generations_without_records_stay_eligible_and_explicit_choices_are_checked(self):
        self.accepted("activate", str(self.new))
        (self.dir / "systemd-run.log").unlink()
        log = self.state / "deployments.log"
        # A failure recorded for another system under generation 1's number (a `nix-env --set` that failed) is not
        # generation 1's: it stays the target, as a generation without any record (the installation's) is.
        with open(log, "a") as handle:
            handle.write(json.dumps({"phase": "finished", "generationAfter": "1", "to": str(self.new), "result": "failed (exit 1)"}) + "\n")
        self.assertIn(f"Checks passed; rollback {self.old}", self.accepted("rollback", "--dry-run").stdout)
        # Generation 1's own last record a failure: nothing left to roll back to without an explicit choice.
        with open(log, "a") as handle:
            handle.write(json.dumps({"phase": "finished", "generationAfter": "1", "to": str(self.old), "result": "failed (exit 4)"}) + "\n")
        self.refused("no generation older than the current one (2) in", "rollback")
        self.assertIn(f"Checks passed; rollback {self.old}", self.accepted("rollback", "--generation", "1", "--dry-run").stdout)
        self.refused("generation 7 does not exist", "rollback", "--generation", "7")
        self.refused("generation 2 is not older than the current generation 2 (the running system's): rollback only goes back", "rollback", "--generation", "2")
        # An explicit choice gets every check of a rollback: here the CMS still running across a release change.
        self.set_unit("terrace-cms.service", "active")
        self.refused("the release changes and terrace-cms.service is active; quiesce first", "rollback", "--generation", "1")
        self.set_unit("terrace-cms.service", "inactive")
        # An unreadable log: a plain rollback refuses (nothing shows which generation failed); status says so.
        with open(log, "a") as handle:
            handle.write("not json\n")
        result = self.run_tool("rollback")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot be read, so nothing shows which generations failed", result.stderr)
        self.assertEqual((self.generation(), os.readlink(self.current), (self.dir / "systemd-run.log").exists()), ("system-2-link", str(self.new), False))
        self.assertIn("previous generation: unknown", self.accepted("status").stdout)
        self.accepted("rollback", "--generation", "1")
        self.assertEqual(os.readlink(self.current), str(self.old))

    def test_an_unfinished_attempt_is_skipped_like_a_failed_one(self):
        self.accepted("activate", str(self.new))
        third = self.system("third", website=NEW_REV, migrations=M_NEW)
        self.accepted("activate", str(third))
        # Generation 3's switch was killed mid-way (power loss): its started line has no finished line after it.
        log = self.state / "deployments.log"
        lines = log.read_text().splitlines()
        log.write_text("".join(line + "\n" for line in lines[:-1]))
        fourth = self.system("fourth", website=NEW_REV, migrations=M_NEW)
        self.current.unlink()
        self.current.symlink_to(third)
        self.accepted("activate", str(fourth))
        self.assertEqual(self.generation(), "system-4-link")
        out = self.accepted("rollback", "--dry-run").stdout
        self.assertRegex(out, r"Skipping generation 3: its last record is 'unfinished \(started [0-9TZ:-]+, never finished\)'")
        self.assertIn(f"Checks passed; rollback {self.new} (", out)
        result = self.accepted("rollback", "--generation", "3", "--dry-run")
        self.assertIn("WARNING: generation 3 was chosen explicitly; its last record: unfinished (started", result.stderr)

    def test_a_switch_unit_stopped_mid_switch_is_not_a_success(self):
        # `systemctl stop terrace-switch.service` mid-switch: systemd-run --wait returns 0, and only the missing finished
        # record shows that the switch never ended (update drill, platform/production/update-drill-test.nix).
        (self.dir / "stop-mid-switch").touch()
        result = self.run_tool("activate", str(self.new))
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn("the terrace-switch unit ended without writing its finished record (stopped or killed mid-switch)", result.stderr)
        self.assertIn("terrace-production: activate FAILED (exit 1).", result.stderr)
        self.assertNotIn("Running generation", result.stdout)
        self.assertEqual([record["phase"] for record in self.records()], ["started"])
        self.assertIn('"phase":"started"', result.stdout, "the client prints the started record it found")
        status = self.accepted("status").stdout
        self.assertIn("WARNING: the last activation started at", status)
        # Control: the same activation, not stopped, succeeds.
        (self.dir / "stop-mid-switch").unlink()
        self.current.unlink()
        self.current.symlink_to(self.old)
        self.accepted("activate", str(self.new))

    def test_rollback_generation_only_goes_back(self):
        self.accepted("activate", str(self.new))
        third = self.system("third", website=NEW_REV, migrations=M_NEW)
        self.accepted("activate", str(third))
        (self.dir / "systemd-run.log").unlink()
        self.accepted("rollback", "--generation", "1")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-1-link", str(self.old)))
        (self.dir / "systemd-run.log").unlink()
        self.refused("generation 3 is not older than the current generation 1 (the running system's): rollback only goes back; to go forward, activate that system: "
                     f"terrace-production activate {third}", "rollback", "--generation", "3")
        # A pending boot default (activate --boot): "current" is the running system's generation, so the pending one is newer.
        self.marker.write_text(NEW_REV + "\n")
        kernel = self.system("kernel", website=NEW_REV, migrations=M_NEW, kernel="2")
        self.accepted("activate", str(kernel), "--boot")
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-4-link", str(self.old)))
        (self.dir / "systemd-run.log").unlink()
        self.refused("generation 4 is not older than the current generation 1", "rollback", "--generation", "4")
        self.refused("generation 3 is not older than the current generation 1", "rollback", "--generation", "3")

    def test_queued_starts_are_waited_for_and_an_unstarted_gate_is_a_failure(self):
        # The gated units' starts come from terrace-production-boot (--no-block): still queued when the switch returns.
        (self.dir / "no-gate-emulation").touch()
        both = "terrace-cms.service loaded failed failed CMS\nterrace-web.service loaded failed failed web\n"
        (self.dir / "on-switch").write_text(f"printf '1 terrace-production.target start waiting\\n2 terrace-cms.service start waiting\\n' >{self.dir}/jobs\n")
        (self.dir / "on-sleep").write_text(f"rm -f {self.dir}/jobs; echo failed >{self.units}/terrace-cms.service; echo failed >{self.units}/terrace-web.service; "
                                           f"printf %s {shlex.quote(both)} >{self.dir}/failed-units\n")
        self.accepted("activate", str(self.new))
        record = self.records()[-1]
        self.assertEqual((record["result"], record["waitTimedOut"], record["notStartedUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", False, []))
        # Nothing started them at all (no job, still inactive) though the gate is closed: never a plain success.
        for name in ("on-switch", "on-sleep", "failed-units"):
            (self.dir / name).unlink()
        for unit in ("terrace-cms.service", "terrace-web.service"):
            self.set_unit(unit, "inactive")
        self.accepted("activate", str(self.old))
        result = self.run_tool("activate", str(self.new))
        record = self.records()[-1]
        self.assertEqual((result.returncode, record["result"], record["notStartedUnits"]),
                         (1, "failed (exit 0, then not started though the gate is closed for this release, THE SITE IS DOWN: terrace-cms.service terrace-web.service)",
                          ["terrace-cms.service", "terrace-web.service"]))
        self.assertIn("THE SITE IS DOWN", result.stderr)
        # Jobs that stay queued past the wait, or a job listing that fails: a timeout, named.
        self.accepted("activate", str(self.old))
        (self.dir / "jobs").write_text("2 terrace-cms.service start waiting\n")
        result = self.run_tool("activate", str(self.new))
        self.assertIn("the wait for the CMS and website timed out after 300 s, still starting: jobs:terrace-cms.service", self.records()[-1]["result"])
        self.assertEqual(result.returncode, 1)
        (self.dir / "jobs").unlink()
        self.accepted("activate", str(self.old))
        (self.dir / "jobs-fail").touch()
        result = self.run_tool("activate", str(self.new))
        self.assertIn("still starting: jobs:unlistable", self.records()[-1]["result"])
        # A gated unit running though the gate is closed (a bypassed gate): never accepted.
        (self.dir / "jobs-fail").unlink()
        self.accepted("activate", str(self.old))
        (self.dir / "on-switch").write_text(f"echo active >{self.units}/terrace-cms.service; echo failed >{self.units}/terrace-web.service; "
                                            f"printf 'terrace-web.service loaded failed failed web\\n' >{self.dir}/failed-units\n")
        result = self.run_tool("activate", str(self.new))
        self.assertEqual((result.returncode, self.records()[-1]["unexpectedUnits"]), (1, ["terrace-cms.service"]))
        (self.dir / "on-switch").unlink()
        self.set_unit("terrace-cms.service", "inactive")
        (self.dir / "failed-units").unlink()
        # The gate open (the marker holds the target's revision) and the units not running: as before, a success (the
        # operator's stopped units stay stopped; nothing here is down because of a gate).
        self.accepted("activate", str(self.old))
        self.marker.write_text(NEW_REV + "\n")
        self.assertEqual(self.accepted("activate", str(self.new)) and self.records()[-1]["result"], "success")

    def test_the_lock_file_is_never_replaced(self):
        # An existing lock file (here with a wrong mode) is opened in place: same inode, made root-only.
        lock = self.state / "deploy.lock"
        lock.touch()
        lock.chmod(0o644)
        inode = lock.stat().st_ino
        self.accepted("activate", str(self.new), "--dry-run")
        self.assertEqual((lock.stat().st_ino, oct(lock.stat().st_mode & 0o777)), (inode, "0o600"))

    def test_the_switch_failed_line_is_read_by_its_exact_text(self):
        # A unit named only in switch-to-configuration's own line fails the gated switch.
        reported = self.system("reported", website=NEW_REV, migrations=M_NEW, switch_rc=4, reported="terrace-cms.service, nginx.service")
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\n")
        self.assertIn(SWITCH_FAILED_LINE + "terrace-cms.service, nginx.service", (reported / "bin" / "switch-to-configuration").read_text())
        result = self.run_tool("activate", str(reported))
        self.assertEqual((result.returncode, self.records()[-1]["unexpectedUnits"]), (1, ["nginx.service"]))
        self.accepted("activate", str(self.old))
        # If upstream changed the text, the line is no longer read: the check is weaker (this unit, failed only in that
        # line, is missed) but a failure the unit listing shows still fails the switch.
        changed = self.system("changed", website=NEW_REV, migrations=M_NEW, switch_rc=4, reported="terrace-cms.service, nginx.service",
                              reported_line="warning: these units failed: ")
        self.accepted("activate", str(changed))
        self.assertEqual(self.records()[-1]["unexpectedUnits"], [])
        self.accepted("activate", str(self.old))
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\nnginx.service loaded failed failed nginx\n")
        result = self.run_tool("activate", str(changed))
        self.assertEqual((result.returncode, self.records()[-1]["unexpectedUnits"]), (1, ["nginx.service"]))

    def test_a_dry_run_before_the_quiesce_reports_not_ready(self):
        # Step 4: the site still runs. The dry run makes every other check and reports the quiesce, exit 3, never 0.
        self.set_unit("terrace-cms.service", "active")
        self.set_unit("terrace-web.service", "active")
        result = self.run_tool("activate", str(self.new), "--dry-run")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("Signatures: 2 new path(s) verified", result.stdout)
        self.assertIn(f"Every other check passed; activate {self.new}", result.stdout)
        self.assertNotIn("Checks passed", result.stdout)
        self.assertIn("dry run NOT READY: the release changes and terrace-cms.service:active terrace-web.service:active still run(s); "
                      "the real command is refused until you stop them: systemctl stop terrace-cms.service terrace-web.service", result.stderr)
        self.assertEqual((self.generation(), os.readlink(self.current), self.records(), (self.dir / "systemd-run.log").exists()), ("system-1-link", str(self.old), [], False))
        # Every other refusal still refuses the dry run (exit 1): an unsigned path, a forward-only ledger.
        (self.dir / "unsigned").write_text(str(self.store / ("b" * 32 + "-cms-new")) + "\n")
        self.refused("not all carry a valid signature", "activate", str(self.new), "--dry-run")
        (self.dir / "unsigned").unlink()
        # A running backup refuses the dry run too: it ends by itself, and the ledger read under it is not the one the switch meets.
        self.set_unit("terrace-backup.service", "active")
        self.refused("terrace-backup.service is active; wait until it has finished", "activate", str(self.new), "--dry-run")
        self.set_unit("terrace-backup.service", "inactive")
        # The real command stays exactly as strict.
        self.refused("the release changes and terrace-cms.service is active; quiesce first", "activate", str(self.new))
        # After the stops (step 6) the dry run is a clean pass.
        self.set_unit("terrace-cms.service", "inactive")
        self.set_unit("terrace-web.service", "inactive")
        result = self.accepted("activate", str(self.new), "--dry-run")
        self.assertIn("Checks passed; activate", result.stdout)
        self.assertNotIn("NOT READY", result.stderr)
        # The same for rollback after a migration: the ledger refusal still wins over the quiesce report.
        self.accepted("activate", str(self.new))
        (self.dir / "systemd-run.log").unlink()
        self.ledger(M_NEW)
        self.marker.write_text(NEW_REV + "\n")
        self.set_unit("terrace-cms.service", "active")
        self.refused("the CMS database has migrations the target release does not have", "rollback", "--dry-run")

    def test_usage(self):
        for args in [("activate",), ("activate", str(self.new), str(self.new)), ("rollback", str(self.new)), ("activate", str(self.new), "--force"),
                     ("rollback", "--generation"), ("rollback", "--generation", "x"), ("rollback", "--generation", "0"), ("activate", str(self.new), "--generation", "1"),
                     ("rollback", "--generation", "1", "--generation", "1")]:
            result = self.run_tool(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("usage: terrace-production activate STORE_PATH", result.stderr)


if __name__ == "__main__":
    unittest.main()
