"""Mutation check for the lease host steps driver: each mutant must make test_lease_host_steps.py fail.

Each mutant is applied to a private copy of the files the driver and its tests read, in a temporary directory; the
checkout is never modified. Run from the repository root: python3 -B infra/nixos/host_steps_mutants.py [WORKERS, default 3]
Exit 0 only if every mutant is killed, none fails to apply, and the unmutated copy passes.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "infra/nixos/lease_host_steps.py"
TEST = "infra/nixos/test_lease_host_steps.py"
HELPER = "infra/cloud/rehearsal_sftp.py"
TESTS = (TEST, "infra/cloud/test_rehearsal_sftp.py")
COPIED = ("infra/nixos/lease_host_steps.py", "infra/nixos/test_lease_host_steps.py", "infra/cloud/rehearsal_sftp.py",
          "infra/cloud/test_rehearsal_sftp.py", "infra/cloud/rehearsal_lease.py",
          "platform/production/terrace-deploy.sh", "platform/website-release.json",
          "platform/production/deploy-previous-release.json")

MUTANTS = (
    # Review round 1 (part B), the fourteen survivors.
    ("the output guard skips standard error", DRIVER,
     '        for name, data in (("stdout", out), ("stderr", err)):\n            rule = self.redactor.rule(data)',
     '        for name, data in (("stdout", out),):\n            rule = self.redactor.rule(data)'),
    ("clean-up takes a surviving process for gone", DRIVER,
     '        gone = process.poll() is not None and not Path(f"/proc/{process.pid}").exists()', "        gone = True"),
    ("a failed SFTP stop ignored", DRIVER,
     '        return left == [] and (result.code == 0 or not (self.run / SFTP / "sftp-listener.json").exists())',
     "        return True"),
    ("a refusal probe accepts exit 0", DRIVER,
     '        result = d.ssh("true", 60, ok=(255,), **kwargs) if "argv" not in kwargs else \\',
     '        result = d.ssh("true", 60, ok=(0, 255), **kwargs) if "argv" not in kwargs else \\'),
    ("the refusal probe's message not checked", DRIVER,
     "        d.expect(re.search(expected, result.err),", "        d.expect(True,"),
    ("V2 not required to carry the spare key only", DRIVER,
     '    d.expect(deploy["signedBy"] == [SPARE_KEY],', "    d.expect(True,"),
    ("write_new replaces an existing file", DRIVER,
     "    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)",
     "    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)"),
    ("the reverse forward's loopback-only check removed", DRIVER,
     '        self.expect(bound and all(word in (f"127.0.0.1:{backup_port}", f"[::1]:{backup_port}") for word in bound),',
     "        self.expect(bound,"),
    ("V5's revert record not checked", DRIVER,
     '             and record5["revert"] == {"from": str(g5), "to": str(g3), "result": "success"}\n', ""),
    ("V4's absent revert not checked", DRIVER, '             and record["revert"] is None and str(record["bootDefaultAfter"]) == str(g4),',
     '             and str(record["bootDefaultAfter"]) == str(g4),'),
    ("the off-host restic check skipped", DRIVER,
     '        check = d.command([*base, "check", "--read-data"], 1800)', '        check = d.command([*base, "snapshots", "--json"], 1800)'),
    ("a reboot accepted without a new boot ID", DRIVER,
     "            if result.code == 0 and BOOT_ID.fullmatch(result.out.strip()) and result.out.strip() != before:",
     "            if result.code == 0 and BOOT_ID.fullmatch(result.out.strip()):"),
    ("the refusal probes not paced", DRIVER, "        time.sleep(PROBE_PAUSE)\n", "        pass\n"),
    ("the wrong-pin file left behind", DRIVER, "    finally:\n        remove_file(wrong)", "    finally:\n        pass"),
    # Review round 2 (part B).
    ("a reboot accepted without reaching running", DRIVER, '        self.expect(state == "running",', "        self.expect(True,"),
    ("5.3: the booted initrd not compared", DRIVER, '    d.expect(len(initrd) == 2 and initrd[0] == initrd[1],', "    d.expect(True,"),
    ("5.3: the refusal's text not checked", DRIVER,
     '        d.expect("initrd differ(s) from the booted system; repeat with --boot" in refused.out,', "        d.expect(True,"),
    ("5.1: the rollback record not checked", DRIVER,
     '    d.expect(rollback["result"] == "success" and rollback["action"] == "rollback" and rollback["to"] == d.variants["v0"],',
     "    d.expect(True,"),
    ("3.2: the push's lines not checked", DRIVER,
     '    d.expect(installed and "Host state: not adopted" in result.out.splitlines(),', "    d.expect(installed,"),
    ("3.2: the staging directory may stay", DRIVER, "    d.expect(not os.path.lexists(d.run / STAGE),", "    d.expect(True,"),
    ("3.3: adoption not checked", DRIVER, '    d.expect("Adopted: the writers start now and at every boot." in result.out,', "    d.expect(True,"),
    ("3.4: the marker not checked", DRIVER, '    d.expect(d.marker() == previous["revision"],', "    d.expect(True,"),
    ("3.4: the ledger not checked", DRIVER, '    d.expect(d.ledger() == previous["migrations"],', "    d.expect(True,"),
    ("3.4: the off-host check skipped", DRIVER, "    offhost = restic_offhost(d, 1)", "    offhost = {}"),
    ("5.2: NOT READY not required", DRIVER,
     '    d.expect("Every other check passed" in first.out and "dry run NOT READY" in first.out,', "    d.expect(True,"),
    ("5.2: the gated record not checked", DRIVER,
     '    d.expect(record["result"].startswith("switched; down until the CMS migration")', '    d.expect(True or record["result"].startswith("switched; down until the CMS migration")'),
    ("5.2: the new ledger not checked", DRIVER, '    d.expect(ledger == pin["migrations"],', "    d.expect(True,"),
    ("5.2: a second migration may change the ledger", DRIVER, '    d.expect(d.ledger() == ledger,', "    d.expect(True,"),
    ("5.4: no reconnect accepted", DRIVER, '    d.expect(reconnect, "the same key reconnects")', '    d.expect(True, "the same key reconnects")'),
    ("5.4: sshd's setting not checked", DRIVER, '    d.expect(loglevel.lower() == "loglevel verbose",', "    d.expect(True,"),
    ("6: the off-host check skipped", DRIVER, '    o["snapshotsOffHost"] = restic_offhost(d, 2)', '    o["snapshotsOffHost"] = {}'),
    ("a store that cannot be loaded ignored", DRIVER, '            failed = "redactor"\n', "            pass\n"),
    ("prepare returns 0 when a scratch file stays", DRIVER,
     "        return EXIT_CLEANUP\n    if code != EXIT_PASS:", "        return EXIT_PASS\n    if code != EXIT_PASS:"),
    ("later signals not ignored", DRIVER,
     "    for sig in SIGNALS:\n        signal.signal(sig, signal.SIG_IGN)\n    raise Interrupted()", "    raise Interrupted()"),
    ("stop_group reports a child that stays", DRIVER, "    return process.poll() is not None\n\n\n# ---- Steps", "    return True\n\n\n# ---- Steps"),
    ("stop_group skips SIGINT", DRIVER, "    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):",
     "    for sig in (signal.SIGTERM, signal.SIGKILL):"),
    ("status without its deploy path line", DRIVER, '        self.expect("deploy path: " in result.out,', "        self.expect(True,"),
    ("a release record of another shape accepted", DRIVER, "        if not isinstance(release, dict) or", "        if False and not isinstance(release, dict) or"),
    ("units left without a receipt not stopped by name", DRIVER,
     '            self.command([systemctl, "--user", "stop", *left], 60, ok=None)', "            pass"),
    ("another run's units counted as this run's", DRIVER, "        return [name for name in loaded if name in names], names",
     "        return loaded, names"),
    # Review round 3 (part B).
    ("unit names from the directory's basename", HELPER,
     "    tag = hashlib.sha256(os.path.realpath(directory).encode()).hexdigest()[:16]",
     "    tag = hashlib.sha256(os.path.basename(directory).encode()).hexdigest()[:16]"),
    ("an interrupted helper exits 0", HELPER, "        return 130\n", "        return 0\n"),
    ("the driver's unit-name shape check removed", DRIVER,
     '        if not names or not all(re.fullmatch(r"terrace-lease-sftp-[0-9a-f]{16}(-watch)?\\.service", n) for n in names):',
     "        if not names:"),
    ("prepare with a private handler that keeps later signals live", DRIVER,
     "    previous = {sig: signal.signal(sig, interrupt_once) for sig in signals}\n    code = EXIT_PASS",
     "    previous = {sig: signal.signal(sig, lambda *_: (_ for _ in ()).throw(Interrupted())) for sig in signals}\n    code = EXIT_PASS"),
    # The rest of review round 1.
    ("restic errors not looked for", DRIVER,
     '        d.expect("no errors were found" in check.out,', "        d.expect(True,"),
    ("status read without its first line", DRIVER,
     "        self.expect(self.STATUS_FIRST.fullmatch(first),", "        self.expect(True,"),
    ("status read whatever its exit", DRIVER,
     '        result = self.root("terrace-production status", 60, ok=(0,))',
     '        result = self.root("terrace-production status", 60, ok=None)'),
    ("the timer guard passes on a partial answer", DRIVER,
     '        self.expect(sorted(fields.get("Id", "") for fields in blocks) == sorted(TIMERS),', "        self.expect(True,"),
    ("the busy-unit check passes on a partial answer", DRIVER,
     "        self.expect(len(lines) == 5 and all(", "        self.expect(True and all("),
    ("curl reads rc files", DRIVER, '[self.tools.curl, "-q", "--noproxy", "*",', '[self.tools.curl, "--noproxy", "*", "-q",'),
    ("curl keeps proxy variables", DRIVER, "env=self.env(without=PROXY_VARIABLES))", "env=self.env())"),
    ("the SFTP start recorded only after it returns", DRIVER, "    d.sftp_attempted = True\n    result = d.command(",
     "    result = d.command("),
    ("one clean-up failure skips the rest", DRIVER,
     "            except Exception as problem:  # noqa: BLE001 - one failure never skips the rest",
     "            except ZeroDivisionError as problem:"),
    ("a dead forward silently accepted", DRIVER,
     '        self.expect(self.alive("reverse-forward") and self.forward_answers(backup_port),', "        self.expect(True,"),
    ("signature names matched as substrings", DRIVER,
     "    return sorted(key for key, name in names.items() if name in found)",
     "    return sorted(key for key, name in names.items() if any(name in item for item in found))"),
    ("the store's values never loaded", DRIVER, "            redactor = load_store_secrets(driver)",
     '            redactor = {"loaded": 0, "short": []}'),
    ("short store values loaded", DRIVER, "                if len(value.strip()) < MIN_SECRET_LENGTH:", "                if False:"),
    ("an interrupted step gets no receipt", DRIVER,
     '            step_receipt(driver, step, "interrupted", started, seconds, "interrupted by a signal", {})\n', ""),
    ("the bootstrap key may equal the operator key", DRIVER, "        if bootstrap == operator:", "        if False:"),
    ("a garbage bootstrap key accepted", DRIVER, "        if bootstrap is None or operator is None:", "        if False:"),
    ("prepare without signal handlers", DRIVER, "    previous = {sig: signal.signal(sig, interrupt_once) for sig in signals}\n    code = EXIT_PASS",
     "    previous = {}\n    code = EXIT_PASS"),
    ("prepare's wipe stops at the first failure", DRIVER,
     "        try:\n            remove_file(scratch / name)\n        except OSError:\n            pass",
     "        remove_file(scratch / name)"),
    ("a missing variant store path accepted", DRIVER, "        if missing:\n", "        if False:\n"),
    ("the summary not written after a receipt failure", DRIVER,
     '    except (OSError, TypeError, ValueError):\n        receipt.update(status="failed"', '    except ZeroDivisionError:\n        receipt.update(status="failed"'),
)


# Every assertion site: each call of the driver's own assertion helper (`d.expect(...)` in a step or helper function,
# `self.expect(...)` in a Driver method) gets one generated mutant whose condition always holds. A site a scripted
# host cannot reach goes here, with its reason; docs/cloud-rehearsal.md lists the same sites.
VM_ONLY = {
}


def assertion_sites(source):
    """(function, line, start offset, end offset, condition text) of each assertion's condition, in source order."""
    tree = ast.parse(source)
    starts = [0]
    for line in source.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    sites = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "expect"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in ("d", "self") and node.args):
                condition = node.args[0]
                start = starts[condition.lineno - 1] + len(source.splitlines(keepends=True)[condition.lineno - 1]
                                                           .encode()[:condition.col_offset].decode())
                end = starts[condition.end_lineno - 1] + len(source.splitlines(keepends=True)[condition.end_lineno - 1]
                                                             .encode()[:condition.end_col_offset].decode())
                sites.append((function.name, condition.lineno, start, end, source[start:end]))
    unique = {}
    for site in sites:
        unique.setdefault(site[2], site)  # a nested function is walked twice
    return sorted(unique.values(), key=lambda site: site[2])


def generated_mutants():
    source = (ROOT / DRIVER).read_text()
    mutants = []
    for function, line, start, end, text in assertion_sites(source):
        key = f"{function}: {' '.join(text.split())[:70]}"
        if key in VM_ONLY:
            continue
        mutants.append((f"assertion always holds, {function} line {line}: {' '.join(text.split())[:60]}", DRIVER,
                        (start, end), "True"))
    return mutants


def copy_tree(target):
    for part in COPIED:
        (target / part).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / part, target / part)


def passes(tree):
    for test in TESTS:
        try:
            result = subprocess.run([sys.executable, "-B", str(tree / test)], cwd=tree, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            return False
        if result.returncode != 0:
            return False
    return True


def check(name, index, title, path, old, new):
    tree = Path(name) / f"m{index}"
    shutil.copytree(Path(name) / "base", tree)
    try:
        source = (tree / path).read_text()
        if isinstance(old, tuple):
            # A generated assertion mutant: an exact source span.
            start, end = old
            (tree / path).write_text(source[:start] + new + source[end:])
        elif source.count(old) != 1:
            return f"NOT APPLIED {index:2d}. {title} (pattern not found exactly once)", None
        else:
            (tree / path).write_text(source.replace(old, new))
        killed = not passes(tree)
        return f"{'killed  ' if killed else 'SURVIVED'} {index:2d}. {title}", killed
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with tempfile.TemporaryDirectory(prefix="host-steps-mutants-") as name:
        base = Path(name) / "base"
        copy_tree(base)
        if not passes(base):
            print("UNMUTATED COPY FAILS: fix the tests first", flush=True)
            return 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            mutants = MUTANTS + tuple(generated_mutants())
            futures = [pool.submit(check, name, index, *mutant) for index, mutant in enumerate(mutants, 1)]
            results = [future.result() for future in futures]
        for line, _ in results:
            print(line, flush=True)
        killed = sum(1 for _, ok in results if ok)
        broken = sum(1 for _, ok in results if ok is None)
        print(f"{killed} of {len(mutants)} mutants killed ({len(mutants) - len(MUTANTS)} generated assertion mutants, "
              f"{len(VM_ONLY)} assertion sites listed as VM only), {broken} not applied", flush=True)
        return 0 if killed == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(main())
