"""terrace-production's own logic (platform/production/terrace-production.sh), with a stub systemctl and tools.

platform/production/base.nix prepends the host's settings and builds the script with writeShellApplication (errexit,
nounset, pipefail); this test does the same with paths in a temporary directory. The stub systemctl keeps each unit's
state in a file and honours the held timers' ConditionPathExists=! the way systemd does (a start whose condition fails
leaves the timer inactive). The production VM proof runs the real script.
Run: python3 -B tests/platform/test_terrace_production.py -v
"""
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
TOOLS_PATH = ":".join(sorted({os.path.dirname(shutil.which(name) or "/bin/" + name) for name in ("bash", "cat", "rm", "install", "chmod", "date", "grep", "tr", "readlink", "jq", "sort", "comm", "tail", "head", "ln")}))
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
  reset-failed) echo "$*" >>"$FAKE_DIR/reset-failed.log" ;;
  list-units) cat "$FAKE_DIR/failed-units" 2>/dev/null || true ;;
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
                  "busy": ["terrace-switch", "terrace-backup", "terrace-backup-check", "terrace-cms-migrate"], "quiesce": ["terrace-cms", "terrace-web"]}
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
  --switch-generation) [[ -L $profile-$4-link ]] || exit 1; ln -sfn "${profile##*/}-$4-link" "$profile" ;;
  *) exit 99 ;;
esac
""",
    # Runs the unit's command in the foreground, as `--wait` would, and logs how it was asked.
    "systemd-run": r"""echo "$*" >>"$FAKE_DIR/systemd-run.log"
while [[ $1 != -- ]]; do shift; done
shift
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
               closure=(), switch_rc=0, manifest=True, switchable=True):
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
                          "websiteRevision": NEW_REV, "result": "success"})
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
        self.assertIn("Generation 2 is the boot default; reboot to run it.", out)
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
            self.refused(f"the release changes and {unit}.service is running; quiesce first", "activate", str(self.new))
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
        self.assertEqual(self.records()[-1]["result"], "failed (exit 1)")
        # As with nixos-rebuild, the generation is registered before the switch: the boot default moved.
        self.assertEqual((self.generation(), os.readlink(self.current)), ("system-2-link", str(self.old)))
        status = self.accepted("status").stdout
        self.assertIn(f"WARNING: the boot default, generation 2 ({broken}), is not the running system: a reboot boots generation 2.", status)
        self.assertIn('"result":"failed (exit 1)"', status)

    def test_a_release_switch_with_the_gate_closed(self):
        # switch-to-configuration exits 4 when units failed: here the CMS (gate) and website (health check) of a release not yet migrated.
        gated = self.system("gated", website=NEW_REV, migrations=M_NEW, switch_rc=4)
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\nterrace-web.service loaded failed failed web\n")
        out = self.accepted("activate", str(gated)).stdout
        self.assertIn("The CMS gate is closed for this release: terrace-cms.service terrace-web.service stay failed until the migration", out)
        self.assertNotIn("Still starting", out)
        record = self.records()[-1]
        self.assertEqual((record["result"], record["failedUnits"], record["startingUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service terrace-web.service", ["terrace-cms.service", "terrace-web.service"], []))
        self.assertIn("reset-failed terrace-cms.service terrace-web.service", (self.dir / "reset-failed.log").read_text())
        # The website can still be retrying its health check (Restart=on-failure) when the switch returns.
        self.accepted("rollback")
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\n")
        self.set_unit("terrace-web.service", "activating")
        out = self.accepted("activate", str(gated)).stdout
        self.assertIn("Still starting without a gate (H7): terrace-web.service; it retries its health check against the stopped CMS and fails within minutes. "
                      "Stop it now: systemctl stop terrace-web.service (step 7).", out)
        record = self.records()[-1]
        self.assertEqual((record["result"], record["failedUnits"], record["startingUnits"]),
                         ("switched; down until the CMS migration: terrace-cms.service", ["terrace-cms.service"], ["terrace-web.service"]))

    def test_other_failed_units_fail_the_switch(self):
        gated = self.system("gated", website=NEW_REV, migrations=M_NEW, switch_rc=4)
        # Another unit failed too: a failed switch, whatever the CMS does.
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\nnginx.service loaded failed failed nginx\n")
        result = self.run_tool("activate", str(gated))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records()[-1]["result"], "failed (exit 4)")
        self.assertEqual(self.records()[-1]["failedUnits"], ["terrace-cms.service", "nginx.service"])
        # The CMS failing where its gate is OPEN (the marker holds the target's revision) is a failed switch too.
        self.marker.write_text(NEW_REV + "\n")
        (self.dir / "failed-units").write_text("terrace-cms.service loaded failed failed CMS\n")
        (self.dir / "systemd-run.log").unlink()
        self.accepted("rollback")
        result = self.run_tool("activate", str(gated))
        self.assertEqual((result.returncode, self.records()[-1]["result"]), (1, "failed (exit 4)"))
        # Exit 4 with no failed unit listed is not explained either.
        (self.dir / "failed-units").unlink()
        self.marker.write_text(OLD_REV + "\n")
        self.accepted("activate", str(self.old))
        result = self.run_tool("activate", str(gated))
        self.assertEqual((result.returncode, self.records()[-1]["result"]), (1, "failed (exit 4)"))

    def test_usage(self):
        for args in [("activate",), ("activate", str(self.new), str(self.new)), ("rollback", str(self.new)), ("activate", str(self.new), "--force")]:
            result = self.run_tool(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("usage: terrace-production activate STORE_PATH", result.stderr)


if __name__ == "__main__":
    unittest.main()
