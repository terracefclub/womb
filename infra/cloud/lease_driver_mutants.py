"""Mutation check for the production lease driver: each mutant must make test_rehearsal_lease_driver.py fail.

Each mutant is applied to a private copy of infra/cloud and infra/nixos in a temporary directory; the checkout is
never modified. Run from the repository root: python3 -B infra/cloud/lease_driver_mutants.py [WORKERS, default 3]
Exit 0 only if every mutant is killed and the unmutated copy passes.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "infra/cloud/rehearsal_lease_driver.py"
LEASE = "infra/cloud/rehearsal_lease.py"

MUTANTS = (
    ("G7 window refusal removed from the plan", DRIVER,
     "    if conflicts:\n        raise Refused(\"G7: the planned lease", "    if False:\n        raise Refused(\"G7: the planned lease"),
    ("G7 window not rechecked at arming", DRIVER,
     "        if conflicts:\n            raise Failed(\"G7: arming now", "        if False:\n            raise Failed(\"G7: arming now"),
    ("G7 at arming ignores the timer's cleanup tail", DRIVER,
     "utc(now + LEASE_SECONDS + TIMER_CLEANUP_SECONDS)", "utc(now + LEASE_SECONDS)"),
    ("two steps reordered: the timer armed after the foundation create", DRIVER,
     "            for current in run_steps(self.cfg):",
     "            for current in sorted(run_steps(self.cfg), key=lambda s: {\"1.1b\": \"1.2c\"}.get(s.id, s.id)):"),
    ("teardown skipped on an interrupt", DRIVER,
     "            verified = self.teardown()\n        except BaseException:",
     "            verified = True if self.interrupted else self.teardown()\n        except BaseException:"),
    ("teardown skipped after a failed step", DRIVER,
     "            verified = self.teardown()\n        except BaseException:",
     "            verified = True if failed else self.teardown()\n        except BaseException:"),
    ("signal handler raises on the first signal", DRIVER,
     "            self.interrupted = True\n            self.say(f\"lease driver: signal {signum}: stopping",
     "            self.interrupted = True\n            raise KeyboardInterrupt\n            self.say(f\"lease driver: signal {signum}: stopping"),
    ("second signal honoured", DRIVER,
     "        elif self.interrupted:\n            self.say(", "        elif self.interrupted:\n            raise KeyboardInterrupt\n            self.say("),
    ("disarm although cleanup did not verify absence", DRIVER,
     "        if code != 0:\n            self.finish_unverified(\"cleanup did not verify absence\")",
     "        if False:\n            self.finish_unverified(\"cleanup did not verify absence\")"),
    ("disarm without the independent inventory", DRIVER,
     "        if code != 0 or not self.guard(\"7.4-compare\", self.inventory_equal, False):", "        if False:"),
    ("final audit failure ignored", DRIVER, "        if not audit_result.get(\"pass\"):", "        if False:"),
    ("final audit ignores the writer units", DRIVER,
     "        facts[\"writerUnitsInactive\"] = self.units_stopped(unit_names(run), \"7.5-show\")",
     "        facts[\"writerUnitsInactive\"] = self.units_stopped(unit_names(run), \"7.5-show\") or True"),
    ("final audit ignores the absence receipt", DRIVER,
     "        facts[\"absence\"] = (absence.get(\"run\") == run", "        facts[\"absence\"] = True or (absence.get(\"run\") == run"),
    ("token deleted although the provider side is not verified", DRIVER,
     "        self.delete(SECRET_FILES + SECRET_DIRECTORIES)\n        self.state[\"kept\"] = list(RECOVERY_KEEP)",
     "        self.delete(SECRET_FILES + SECRET_DIRECTORIES + (TOKEN,))\n        self.state[\"kept\"] = list(RECOVERY_KEEP)"),
    ("deletion stops at the first error", DRIVER,
     "                self.state[\"deleteFailures\"].append(f\"{name}: {type(problem).__name__}\")", "                raise"),
    ("local clean-up ignored in the exit code", DRIVER, "        if not verified or self.problems:", "        if not verified:"),
    ("failed SFTP stop not noted", DRIVER,
     "            if code != 0:\n                self.problems.append(\"the throwaway SFTP",
     "            if False:\n                self.problems.append(\"the throwaway SFTP"),
    ("unit state read fails open", DRIVER,
     "        if outcome.code != 0 or not re.fullmatch(r\"[a-z-]+\", value):", "        if False:"),
    ("recovery text not printed before the closing writes", DRIVER,
     "            self.say(recovery_text(self.cfg, summary), self.err)  # before any write (M1)", "            pass"),
    ("one retry after a failed step", DRIVER,
     "                outcome = self.run_child(argv, bound, label=step.id, grace=step.grace)",
     "                outcome = self.run_child(argv, bound, label=step.id, grace=step.grace)\n"
     "                if outcome.code:\n                    outcome = self.run_child(argv, bound, label=step.id, grace=step.grace)"),
    ("every child bound x100", DRIVER, "        result = self.runner.run(argv, self.cfg.bound(bound),",
     "        result = self.runner.run(argv, self.cfg.bound(bound) * 100,"),
    ("cleanup bound x100", DRIVER, "[\"cleanup\", *creds], 420)", "[\"cleanup\", *creds], 42000)"),
    ("children inherit the whole environment", DRIVER, "                                 env=self.cfg.child_env(),",
     "                                 env=None,"),
    ("host-steps exit 3, a death by signal or no status not escalated", DRIVER,
     "                    if outcome.code not in HOST_VERIFIED or not outcome.gone:", "                    if False:"),
    ("plan digest ignores the steps", DRIVER,
     "(\"schema\", \"inputs\", \"steps\", \"teardown\", \"cost\")", "(\"schema\", \"inputs\", \"teardown\", \"cost\")"),
    ("plan digest ignores the teardown list", DRIVER,
     "(\"schema\", \"inputs\", \"steps\", \"teardown\", \"cost\")", "(\"schema\", \"inputs\", \"steps\", \"cost\")"),
    ("plan digest ignores the tools", DRIVER, "\"tools\": tools,", "\"tools\": {},"),
    ("preflight drift against the stated baseline dropped", DRIVER,
     "        if counts != self.cfg.expect_baseline:", "        if False:"),
    ("lease baseline not compared with the preflight", DRIVER,
     "        if manifest[\"baseline\"] != self.ctx[\"preflight_baseline\"]:", "        if False:"),
    ("person check skips the equal inventory", DRIVER,
     "    equal = code == 0 and self.guard(\"7.4-compare\", self.inventory_equal, False)", "    equal = True"),
    ("person check does not stop the SFTP endpoint", DRIVER,
     "    self.guard(\"7.2\", self.stop_sftp)\n    if not self.units_stopped(unit_names(self.cfg.run), \"resume-units\"):",
     "    if not self.units_stopped(unit_names(self.cfg.run), \"resume-units\"):"),
    ("root not refused", DRIVER, "    if running_as_root():\n        raise Refused(\"refusing to run as root:",
     "    if False:\n        raise Refused(\"refusing to run as root:"),
    ("G5 diagnostic-archive refusal removed", DRIVER, "\"diagnostic\" in str(", "\"diagnostic-never\" in str("),
    ("G5 archive provenance check removed", DRIVER,
     "    if archive != store / Path(expected).name", "    if False and archive != store / Path(expected).name"),
    ("G6 store validity check removed", DRIVER,
     "    if code:\n        raise Refused(\"G6: a prestaged", "    if False:\n        raise Refused(\"G6: a prestaged"),
    ("open lease pointer check removed", DRIVER,
     "        if entry.name.endswith(\".open\") and", "        if False and entry.name.endswith(\".open\") and"),
    ("own open pointer check removed", DRIVER,
     "    if os.path.lexists(root / (R.name + \".open\")) and not at_run:", "    if False:"),
    ("token copy may sit outside the run directory", DRIVER, "    if (token.parent != R or ", "    if (False or "),
    ("deletion follows a link out of the run directory", DRIVER,
     "    if os.path.realpath(path.parent) != str(path.parent):", "    if False:"),
    ("the token passed by value instead of by path", DRIVER,
     "    R, T = cfg.run_directory, str(cfg.credential_file)",
     "    R, T = cfg.run_directory, cfg.credential_file.read_text()"),
    ("binding refresher left running across the lock-taking boot step", DRIVER,
     "        elif not wanted:\n            self.stop_refresher()", "        elif False:\n            self.stop_refresher()"),
    ("binding refresh failure ignored", DRIVER,
     "            if outcome.code != 0:\n                self.refresh_failed = True",
     "            if False:\n                self.refresh_failed = True"),
    ("deadline guard removed", DRIVER, "                if bound < (step.min_bound or step.bound):",
     "                if False:"),
    ("availability check may write (a POST)", LEASE,
     "            body = self.request(endpoint + \"?\" + urlencode({**(query or {}), \"per_page\": 50, \"page\": page}))",
     "            body = self.request(endpoint + \"?\" + urlencode({**(query or {}), \"per_page\": 50, \"page\": page}), post={})"),
    ("availability ignores the location", LEASE,
     "        if row[\"location\"][\"name\"] != location:\n            continue", "        pass"),
)
TESTS = ("infra/cloud/test_rehearsal_lease_driver.py", "infra/cloud/test_rehearsal_lease.py")


def copy_tree(target):
    for part in ("infra/cloud", "infra/nixos"):
        shutil.copytree(ROOT / part, target / part, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))


def passes(tree):
    for test in TESTS:
        result = subprocess.run([sys.executable, "-B", str(tree / test)], cwd=tree, capture_output=True, timeout=600)
        if result.returncode:
            return False
    return True


def check(name, index, title, path, old, new):
    tree = Path(name) / f"m{index}"
    shutil.copytree(Path(name) / "base", tree)
    try:
        source = (tree / path).read_text()
        if source.count(old) != 1:
            return f"NOT APPLIED {index:2d}. {title} (pattern not found exactly once)", False
        (tree / path).write_text(source.replace(old, new))
        killed = not passes(tree)
        return f"{'killed  ' if killed else 'SURVIVED'} {index:2d}. {title}", killed
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with tempfile.TemporaryDirectory(prefix="lease-mutants-") as name:
        base = Path(name) / "base"
        copy_tree(base)
        if not passes(base):
            print("UNMUTATED COPY FAILS: fix the tests first", flush=True)
            return 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(check, name, index, *mutant) for index, mutant in enumerate(MUTANTS, 1)]
            results = [future.result() for future in futures]
        for line, _ in results:
            print(line, flush=True)
        killed = sum(1 for _, ok in results if ok)
        print(f"{killed} of {len(MUTANTS)} mutants killed", flush=True)
        return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
