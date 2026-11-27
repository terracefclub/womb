"""Mutation check for the production lease driver: each mutant must make test_rehearsal_lease_driver.py fail.

Each mutant is applied to a private copy of infra/cloud and infra/nixos in a temporary directory; the checkout is
never modified. Run from the repository root: python3 -B infra/cloud/lease_driver_mutants.py
Exit 0 only if every mutant is killed and the unmutated copy passes.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "infra/cloud/rehearsal_lease_driver.py"
LEASE = "infra/cloud/rehearsal_lease.py"

MUTANTS = (
    ("G7 window refusal removed from the plan", DRIVER,
     "    if conflicts:\n        raise Refused(\"G7: the planned lease", "    if False:\n        raise Refused(\"G7: the planned lease"),
    ("G7 window not rechecked at arming", DRIVER,
     "        if conflicts:\n            raise Failed(\"G7: arming now", "        if False:\n            raise Failed(\"G7: arming now"),
    ("two steps reordered: the timer armed after the foundation create", DRIVER,
     "            for current in steps:",
     "            for current in sorted(steps, key=lambda s: {\"1.1b\": \"1.2c\"}.get(s.id, s.id)):"),
    ("teardown skipped on an interrupt", DRIVER,
     "                verified = self.teardown()", "                verified = True if self.interrupted else self.teardown()"),
    ("teardown skipped after a failed step", DRIVER,
     "                verified = self.teardown()", "                verified = True if failed else self.teardown()"),
    ("disarm although cleanup did not verify absence", DRIVER,
     "                                cfg.py(\"infra\", \"cloud\", \"rehearsal_lease.py\") + [\"cleanup\", *creds], 420)\n"
     "            if code:\n                return False",
     "                                cfg.py(\"infra\", \"cloud\", \"rehearsal_lease.py\") + [\"cleanup\", *creds], 420)\n"
     "            if False:\n                return False"),
    ("disarm without the independent inventory", DRIVER,
     "            if code or not self.inventory_equal():", "            if False:"),
    ("material deleted although teardown escalated", DRIVER,
     "            if code:\n                return False\n            audit = self.audit_dir",
     "            if code:\n                self.delete_material()\n                return False\n            audit = self.audit_dir"),
    ("one retry after a failed step", DRIVER,
     "                code, output = self.runner.run(argv, bound, label=step.id, grace=step.grace)",
     "                code, output = self.runner.run(argv, bound, label=step.id, grace=step.grace)\n"
     "                if code:\n                    code, output = self.runner.run(argv, bound, label=step.id, grace=step.grace)"),
    ("G5 diagnostic-archive refusal removed", DRIVER, "\"diagnostic\" in str(", "\"diagnostic-never\" in str("),
    ("G5 archive provenance check removed", DRIVER,
     "    if archive != store / Path(expected).name", "    if False and archive != store / Path(expected).name"),
    ("G6 store validity check removed", DRIVER,
     "    if code:\n        raise Refused(\"G6: a prestaged", "    if False:\n        raise Refused(\"G6: a prestaged"),
    ("open lease pointer check removed", DRIVER,
     "        if entry.name.endswith(\".open\") and", "        if False and entry.name.endswith(\".open\") and"),
    ("deletion follows a link out of the run directory", DRIVER,
     "    if os.path.realpath(path.parent) != str(path.parent):", "    if False:"),
    ("the token passed by value instead of by path", DRIVER,
     "    R, T = cfg.run_directory, str(cfg.credential_file)",
     "    R, T = cfg.run_directory, cfg.credential_file.read_text()"),
    ("binding refresher left running across the lock-taking boot step", DRIVER,
     "        elif not wanted:\n            self.stop_refresher()", "        elif False:\n            self.stop_refresher()"),
    ("deadline guard removed", DRIVER, "                if bound < (step.min_bound or step.bound):",
     "                if False:"),
    ("host-steps exit 3 not escalated", DRIVER, " or self.host_exit == 3", ""),
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


def main():
    with tempfile.TemporaryDirectory(prefix="lease-mutants-") as name:
        base = Path(name) / "base"
        copy_tree(base)
        if not passes(base):
            print("UNMUTATED COPY FAILS: fix the tests first")
            return 1
        survivors = 0
        for index, (title, path, old, new) in enumerate(MUTANTS, 1):
            tree = Path(name) / f"m{index}"
            shutil.copytree(base, tree)
            source = (tree / path).read_text()
            if source.count(old) != 1:
                print(f"MUTANT {index} NOT APPLIED (pattern not found once): {title}")
                survivors += 1
                continue
            (tree / path).write_text(source.replace(old, new))
            killed = not passes(tree)
            survivors += not killed
            print(f"{'killed  ' if killed else 'SURVIVED'} {index:2d}. {title}")
            shutil.rmtree(tree)
        print(f"{len(MUTANTS) - survivors} of {len(MUTANTS)} mutants killed")
        return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
