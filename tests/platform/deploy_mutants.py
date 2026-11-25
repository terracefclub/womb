"""Mutation check for the deploy path's guards: each mutant removes one guard (or the switch unit, or the record)
from platform/production/terrace-production.sh or platform/production/terrace-deploy.sh, and the unit tests must
FAIL against it. A mutant that passes means its guard is untested. The unmutated scripts must pass.

Run: python3 -B tests/platform/deploy_mutants.py   (exit 0 only if every mutant is killed; about two minutes)
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = ROOT / "platform/production/terrace-production.sh"
DEPLOY = ROOT / "platform/production/terrace-deploy.sh"
# (name, script, exact text, replacement)
MUTANTS = [
    ("root only", PRODUCTION, "[[ $(id -u) == 0 ]] ||", "true ||"),
    ("deploy path off without keys", PRODUCTION, "(( ${#deploy_keys[@]} )) || deny 'no deploy key", "true || deny 'no deploy key"),
    ("store path form", PRODUCTION, '|| deny "$target is not a store path', '|| true "$target is not a store path'),
    ("exists, not a link", PRODUCTION, '|| deny "$target does not exist', '|| true "$target does not exist'),
    ("valid in the store", PRODUCTION, '|| deny "$target is not a valid path', '|| true "$target is not a valid path'),
    ("NixOS system", PRODUCTION, '|| deny "$target is not a NixOS system', '|| true "$target is not a NixOS system'),
    ("manifest present", PRODUCTION, '|| deny "$target has no Terrace system manifest', '|| true "$target has no Terrace system manifest'),
    ("role, host, instance", PRODUCTION, '|| deny "$target was built for', '|| true "$target was built for'),
    ("not the running system", PRODUCTION, '|| deny "$target is already the running system"', '|| true'),
    ("no switch, migration or backup running", PRODUCTION, "case $state in active | activating", "case $state in never-matches"),
    ("signatures", PRODUCTION, "if ! out=$(nix", "if false && ! out=$(nix"),
    ("kernel needs --boot", PRODUCTION, 'deny "${changed[*]} differ(s)', 'true "${changed[*]} differ(s)'),
    ("PostgreSQL major", PRODUCTION, '|| deny "PostgreSQL major', '|| true "PostgreSQL major'),
    ("upstream schema change (U3)", PRODUCTION, "(( accept_upstream )) || deny", "true || deny"),
    ("quiesce before a new release", PRODUCTION, '|| deny "the release changes and', '|| true "the release changes and'),
    ("ledger needs PostgreSQL", PRODUCTION, 'deny "PostgreSQL is not running', 'return 0 "PostgreSQL is not running'),
    ("failed migration never rolls back (H9)", PRODUCTION, 'deny "the CMS ledger records migrations', 'true "the CMS ledger records migrations'),
    ("forward-only needs --cms-stays-down", PRODUCTION, "(( cms_stays_down )) || deny", "true || deny"),
    ("target migration list known", PRODUCTION, '|| deny "$target records no CMS migration list', '|| true "$target records no CMS migration list'),
    ("rollback goes to an OLDER generation", PRODUCTION, "(( n < $1 ))", "(( n != $1 ))"),
    ("switch runs in the terrace-switch unit", PRODUCTION, "systemd-run --unit=terrace-switch --collect", "systemd-run --unit=other --collect"),
    ("result recorded", PRODUCTION, "}' >>\"$records\"", "}' >/dev/null"),
    ("helper: secret key only", DEPLOY, "grep -Eq '^[A-Za-z0-9._-]+:[A-Za-z0-9+/]{86}==$' \"$key\" ||", "true ||"),
    ("helper: pinned host key", DEPLOY, "-o StrictHostKeyChecking=yes", "-o StrictHostKeyChecking=no"),
    ("helper: sign before copying", DEPLOY, "store sign --key-file \"$key\" --recursive \"$toplevel\"", "store sign --key-file \"$key\" --recursive \"$toplevel\" || true"),
]
TESTS = {PRODUCTION: (ROOT / "tests/platform/test_terrace_production.py", "TERRACE_PRODUCTION_SCRIPT", ["DeployPath"]),
         DEPLOY: (ROOT / "tests/platform/test_terrace_deploy.py", "TERRACE_DEPLOY_SCRIPT", [])}


def run(script_path, original, extra_env):
    test, variable, names = TESTS[original]
    env = dict(os.environ, **{variable: str(script_path)}, **extra_env)
    return subprocess.run([sys.executable, "-B", str(test), *names], capture_output=True, text=True, env=env, timeout=900).returncode


def mutant(entry, directory):
    name, original, old, new = entry
    text = original.read_text()
    if text.count(old) != 1:
        return name, f"BROKEN: the text occurs {text.count(old)} times"
    path = Path(directory) / (name.replace(" ", "-").replace("/", "-") + original.suffix)
    path.write_text(text.replace(old, new))
    path.chmod(0o700)
    return name, "killed" if run(path, original, {}) != 0 else "SURVIVED"


def main():
    for original in TESTS:
        if run(original, original, {}) != 0:
            print(f"the unmutated {original.name} fails its tests; fix that first")
            return 1
    with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda entry: mutant(entry, directory), MUTANTS))
    for name, outcome in results:
        print(f"{outcome:9} {name}")
    survivors = [name for name, outcome in results if outcome != "killed"]
    print(f"{len(results) - len(survivors)} of {len(results)} mutants killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
