"""Mutation check for the lease host steps driver: each mutant must make test_lease_host_steps.py fail.

Each mutant is applied to a private copy of the files the driver and its tests read, in a temporary directory; the
checkout is never modified. Run from the repository root: python3 -B infra/nixos/host_steps_mutants.py [WORKERS, default 3]
Exit 0 only if every mutant is killed, none fails to apply, and the unmutated copy passes.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DRIVER = "infra/nixos/lease_host_steps.py"
TEST = "infra/nixos/test_lease_host_steps.py"
COPIED = ("infra/nixos/lease_host_steps.py", "infra/nixos/test_lease_host_steps.py", "infra/cloud/rehearsal_sftp.py",
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
     '                return left == [] and (result.code == 0 or not (self.run / SFTP / "sftp-listener.json").exists())',
     "                return True"),
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
    ("prepare without signal handlers", DRIVER, "    previous = {sig: signal.signal(sig, stop) for sig in signals}\n    code = EXIT_PASS",
     "    previous = {}\n    code = EXIT_PASS"),
    ("prepare's wipe stops at the first failure", DRIVER,
     "        try:\n            remove_file(scratch / name)\n        except OSError:\n            pass",
     "        remove_file(scratch / name)"),
    ("a missing variant store path accepted", DRIVER, "        if missing:\n", "        if False:\n"),
    ("the summary not written after a receipt failure", DRIVER,
     '    except (OSError, TypeError, ValueError):\n        receipt.update(status="failed"', '    except ZeroDivisionError:\n        receipt.update(status="failed"'),
)


def copy_tree(target):
    for part in COPIED:
        (target / part).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / part, target / part)


def passes(tree):
    try:
        result = subprocess.run([sys.executable, "-B", str(tree / TEST)], cwd=tree, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def check(name, index, title, path, old, new):
    tree = Path(name) / f"m{index}"
    shutil.copytree(Path(name) / "base", tree)
    try:
        source = (tree / path).read_text()
        if source.count(old) != 1:
            return f"NOT APPLIED {index:2d}. {title} (pattern not found exactly once)", None
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
            futures = [pool.submit(check, name, index, *mutant) for index, mutant in enumerate(MUTANTS, 1)]
            results = [future.result() for future in futures]
        for line, _ in results:
            print(line, flush=True)
        killed = sum(1 for _, ok in results if ok)
        broken = sum(1 for _, ok in results if ok is None)
        print(f"{killed} of {len(MUTANTS)} mutants killed, {broken} not applied", flush=True)
        return 0 if killed == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
