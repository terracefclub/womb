"""Mutation check for the deploy path's guards: each mutant removes one guard (or the switch unit, the lock, a record)
from platform/production/terrace-production.sh or platform/production/terrace-deploy.sh, and the unit tests must
FAIL against it. A mutant that passes means its guard is untested. The unmutated scripts must pass.

Run: python3 -B tests/platform/deploy_mutants.py   (exit 0 only if every mutant is killed; several minutes)
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
OTHER_UNIT = """      elif [[ $active == failed || $sub == auto-restart ]]; then
        unexpected+=("$name")"""
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
    ("no switch, migration, sweep or backup running", PRODUCTION, 'case $state in active | activating | deactivating | reloading) deny "$unit.service is', 'case $state in never-matches) deny "$unit.service is'),
    ("no operator tool running", PRODUCTION, "[[ -z $tools ]] || deny", "true || deny"),
    ("operator tools must be listable", PRODUCTION, "'terrace-cms-tool-*') || deny", "'terrace-cms-tool-*') || true"),
    ("signatures", PRODUCTION, "if ! out=$(nix", "if false && ! out=$(nix"),
    ("kernel needs --boot", PRODUCTION, 'deny "${changed[*]} differ(s)', 'true "${changed[*]} differ(s)'),
    ("PostgreSQL version present (app)", PRODUCTION, "[[ -n $cms_marker && ( -z $from || -z $to ) ]] && deny", "[[ -n $cms_marker && ( -z $from || -z $to ) ]] && true"),
    ("PostgreSQL major", PRODUCTION, '|| deny "PostgreSQL major', '|| true "PostgreSQL major'),
    ("upstream schema change (U3)", PRODUCTION, "(( accept_upstream )) || deny", "true || deny"),
    ("quiesce before a new release", PRODUCTION, "      active | activating | reloading)\n        # A dry run", "      never-matches)\n        # A dry run"),
    ("quiesce counts a restarting unit", PRODUCTION, "      active | activating | reloading)\n        # A dry run", "      active)\n        # A dry run"),
    ("ledger needs PostgreSQL", PRODUCTION, 'deny "PostgreSQL is not running', 'return 0 "PostgreSQL is not running'),
    ("failed migration never rolls back (H9)", PRODUCTION, 'deny "the CMS ledger records migrations', 'true "the CMS ledger records migrations'),
    ("forward-only needs --cms-stays-down", PRODUCTION, "(( cms_stays_down )) || deny", "true || deny"),
    ("target migration list known", PRODUCTION, '|| deny "$target records no CMS migration list', '|| true "$target records no CMS migration list'),
    ("rollback goes to an OLDER generation", PRODUCTION, "(( n < $1 )) || continue", "(( n != $1 )) || continue"),
    ("rollback skips a failed generation", PRODUCTION, "if [[ $result == failed* ]]; then skipped+=", "if false; then skipped+="),
    ("a failure counts only for that generation's system", PRODUCTION, 'select(.phase == "finished" and .generationAfter == $n and .to == $to)', 'select(.phase == "finished" and .generationAfter == $n)'),
    ("rollback refuses when every older generation failed", PRODUCTION, '[[ -n $rollback_gen ]] || deny', "[[ -n $rollback_gen ]] || exit 0"),
    ("an unreadable log refuses a plain rollback", PRODUCTION, 'rollback_target "$(gen_of)" || deny', 'rollback_target "$(gen_of)" || true'),
    ("rollback --generation is used", PRODUCTION, "      generation=$chosen\n", "      generation=$(gen_of)\n"),
    ("rollback --generation must exist", PRODUCTION, '[[ -L $profile-$chosen-link ]] || deny', "true || deny"),
    ("rollback --generation not the current one", PRODUCTION, '[[ $chosen != "$(gen_of)" ]] || deny', "true || deny"),
    ("rollback --generation gets the checks", PRODUCTION, "    check_cms \"$target\"\n    if (( ${#not_quiesced[@]} ))", "    [[ -n $chosen ]] || check_cms \"$target\"\n    if (( ${#not_quiesced[@]} ))"),
    ("lock file never replaced", PRODUCTION, 'exec 9>>"$lock"', 'install -m 0600 /dev/null "$lock"; exec 9>>"$lock"'),
    ("lock file made root-only in place", PRODUCTION, '  chmod 0600 "$lock"\n', "\n"),
    ("the switch's line is read by its exact text", PRODUCTION, "line=$(grep -m 1 '^warning: the following units failed: '", "line=$(grep -m 1 '^warning: the following units have failed: '"),
    ("the record waits for the gated units to settle", PRODUCTION, "    settle\n    while read", "    while read"),
    ("a gated unit still starting after the wait fails the switch", PRODUCTION, 'if [[ $active == failed ]]; then failed+=("$name"); else unexpected+=("$name"); fi', 'if [[ $active == failed ]]; then failed+=("$name"); fi'),
    ("the wait is bounded", PRODUCTION, "(( ! moving || waited >= settle_seconds ))", "(( ! moving || waited >= 3 * settle_seconds ))"),
    ("the real run refuses before the quiesce", PRODUCTION, "        if (( dry_run )); then\n          not_quiesced+=", "        if true; then\n          not_quiesced+="),
    ("a dry run before the quiesce is not a clean pass", PRODUCTION, "      exit 3\n", "      return 0\n"),
    ("a dry run before the quiesce is reported", PRODUCTION, "    if (( ${#not_quiesced[@]} )); then\n      echo \"Every other check passed;", "    if false; then\n      echo \"Every other check passed;"),
    ("one command at a time (lock)", PRODUCTION, "flock -n 9 ||", "true ||"),
    ("the unit re-checks the state", PRODUCTION, '[[ $from == "$expected_from" && $before == "$expected_gen" ]] ||', "true ||"),
    ("rollback cancels a pending boot default", PRODUCTION, "if [[ $action == rollback && -z $chosen && $(gen_of) != none", "if [[ $action == never && -z $chosen && $(gen_of) != none"),
    ("dry run changes nothing", PRODUCTION, "  if (( dry_run )); then\n    echo 'Dry run: nothing changed.'", "  if false; then\n    echo 'Dry run: nothing changed.'"),
    ("switch runs in the terrace-switch unit", PRODUCTION, "systemd-run --unit=terrace-switch --collect", "systemd-run --unit=other --collect"),
    ("only this run's records are printed", PRODUCTION, 'last=$(tail -n +"$((lines + 1))" "$records")', 'last=$(cat "$records")'),
    ("result recorded", PRODUCTION, "unexpectedUnits: $unexpected}'", "unexpectedUnits: $unexpected} | empty'"),
    ("started record", PRODUCTION, "'{phase: \"started\"", "'empty | {phase: \"started\""),
    ("status names an unfinished activation", PRODUCTION, '<<<"$last") == started ]]', '<<<"$last") == never ]]'),
    ("pre-existing failures recorded", PRODUCTION, 'then prefailed+=("$unit"); fi', "then :; fi"),
    ("a gated switch needs the gate closed for the target", PRODUCTION, '[[ -n $cms_marker && $marker != "$(field "$target" .website.revision)" && ${#failed[@]} -gt 0', "[[ -n $cms_marker && ${#failed[@]} -gt 0"),
    ("any other failed unit fails a gated switch", PRODUCTION, OTHER_UNIT, OTHER_UNIT.replace('unexpected+=("$name")', ":")),
    ("a unit in auto-restart fails a gated switch", PRODUCTION, "elif [[ $active == failed || $sub == auto-restart ]]; then", "elif [[ $active == failed ]]; then"),
    ("non-service units count", PRODUCTION, "systemctl list-units --all --plain --no-legend --full)", "systemctl list-units --all --plain --no-legend --full | grep '[.]service ')"),
    ("the switch's own failed list counts", PRODUCTION, '|| unexpected+=("$name")', "|| true"),
    ("helper: secret key only", DEPLOY, "grep -Eq '^[A-Za-z0-9._-]+:[A-Za-z0-9+/]{86}==$' \"$key\" ||", "true ||"),
    ("helper: a symlinked key is judged by its target", DEPLOY, 'mode=$(stat -L -c %a "$key" 2>/dev/null || stat -L -f %Lp "$key")', 'mode=$(stat -c %a "$key" 2>/dev/null || stat -f %Lp "$key")'),
    ("helper: key not readable by others", DEPLOY, "(( (8#$mode & 8#077) == 0 )) ||", "true ||"),
    ("helper: known hosts required", DEPLOY, "[[ -n $key && -n $address && -n $identity && -n $known_hosts && -n $toplevel ]]", "[[ -n $key && -n $address && -n $identity && -n $toplevel ]]"),
    ("helper: pinned host key", DEPLOY, "-o StrictHostKeyChecking=yes", "-o StrictHostKeyChecking=no"),
    ("helper: the store path, not ./result", DEPLOY, 'toplevel=$(readlink -f "$toplevel")', "true"),
    ("helper: sign before copying", DEPLOY, "store sign --key-file \"$key\" --recursive \"$toplevel\"", "store sign --key-file \"$key\" --recursive \"$toplevel\" || true"),
]
TESTS = {PRODUCTION: (ROOT / "tests/platform/test_terrace_production.py", "TERRACE_PRODUCTION_SCRIPT", ["DeployPath"]),
         DEPLOY: (ROOT / "tests/platform/test_terrace_deploy.py", "TERRACE_DEPLOY_SCRIPT", [])}


def run(script_path, original, extra_env):
    test, variable, names = TESTS[original]
    env = dict(os.environ, **{variable: str(script_path)}, **extra_env)
    return subprocess.run([sys.executable, "-B", str(test), *names], capture_output=True, text=True, env=env, timeout=1800).returncode


def mutant(entry, directory):
    name, original, old, new = entry
    text = original.read_text()
    if text.count(old) != 1:
        return name, f"BROKEN: the text occurs {text.count(old)} times"
    path = Path(directory) / (name.replace(" ", "-").replace("/", "-").replace("'", "") + original.suffix)
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
