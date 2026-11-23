#!/usr/bin/env python3
"""Bounded established-service rehearsal and explicit run-scoped synthetic purge."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys

from host import HERE, contained_driver, create_run, fingerprint, owned_guests, unit_off, write_json


STORE = '/nix/store/'


def private_json(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError()
        return json.loads(os.read(fd, limit + 1))
    finally:
        os.close(fd)


def checked_run(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError()
    run = path.resolve(strict=True)
    info = run.stat()
    receipt = private_json(run / 'receipt.json')
    run_id = receipt.get('runId', '')
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError()
    if not re.fullmatch('[a-f0-9]{32}', run_id) or run.name != 'run-' + run_id or receipt.get('scope') != 'terrace-disposable-synthetic':
        raise ValueError()
    return run, run_id, receipt


class Refused(ValueError):
    """A refusal whose reason is a fixed text without paths or values; the command line prints it."""


def require_controller_gone(run, run_id):
    # A failed or incomplete run is purged only when the controller that created it has exited.
    # Runs record it in controller.json (since the failed-run purge); the driver phase also in control.json.
    # Every record present must be well formed and name a process that is not running.
    names = [name for name in ['controller.json', 'control.json'] if os.path.lexists(run / name)]
    if not names:
        raise Refused('no controller record (controller.json or control.json)')
    for name in names:
        try:
            record = private_json(run / name, 4096)
        except (OSError, ValueError, RecursionError):
            # A link, a public or foreign file, a non-file, unparsable JSON or JSON nested too deeply to read.
            raise Refused('malformed controller record ' + name) from None
        if not isinstance(record, dict):
            raise Refused('malformed controller record ' + name)
        pid, start = record.get('controllerPid'), record.get('controllerStart')
        if record.get('runId') != run_id or type(pid) is not int or pid <= 0 or not isinstance(start, str) or not start:
            raise Refused('malformed controller record ' + name)
        try:
            current = fingerprint(pid)
        except ProcessLookupError:
            # The process exited while its start time was read: no process has this id now, so it is not
            # the controller (a later process reusing the id has a different start time and counts as gone too).
            current = None
        except (OSError, IndexError):
            # Unreadable process state, or a /proc/PID/stat line without the expected fields.
            raise Refused('cannot tell whether the controller in ' + name + ' is running') from None
        if current == start:
            raise Refused('the controller in ' + name + ' is still running')


def purge(path, confirmed=False, failed=False):
    if not confirmed:
        raise ValueError()
    run, run_id, receipt = checked_run(path)
    unit = 'terrace-rehearsal-' + run_id + '.service'
    if failed:
        # A failed or incomplete run (no complete receipt): its compute must be provably off now,
        # and its controller gone. A complete run uses the ordinary purge.
        if receipt.get('complete') is not False:
            raise Refused('the receipt is not that of a failed or incomplete run')
        if not unit_off(unit) or owned_guests(run_id):
            raise Refused('the run unit or a tagged guest is still running')
        require_controller_gone(run, run_id)
    # Otherwise only a completed, verified synthetic rehearsal.
    elif receipt.get('complete') is not True or receipt.get('computeOff') is not True or not unit_off(unit) or owned_guests(run_id):
        raise ValueError()
    # A run that failed before its guardian started has no runtime directory record.
    record = private_json(run / 'runtime.json', 4096) if not failed or os.path.lexists(run / 'runtime.json') else None
    directory = Path(record.get('directory', '')) if record is not None else None
    if record is not None and (record.get('scope') != receipt['scope'] or record.get('runId') != run_id):
        raise ValueError()
    if directory is not None and (directory.parent != Path('/tmp') or not directory.name.startswith('tr-' + run_id[:8] + '-') or directory.is_symlink()):
        raise ValueError()
    if directory is not None and directory.exists():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError()
        if private_json(directory / 'owner.json', 4096) != {'scope': receipt['scope'], 'runId': run_id}:
            raise ValueError()
    # Validate the whole deletion set BEFORE mutating anything. Symlink roots
    # cannot redirect traversal. Only the run's dedicated directories are owned.
    targets = [run / name for name in ['state', 'retained', 'escrow']]
    # A run that died without cleanup may leave its driver GC root: only a symlink
    # named driver-root directly inside the run directory is ever removed.
    root = run / 'driver-root'
    if os.path.lexists(root) and not root.is_symlink():
        raise ValueError()
    for target in targets:
        if target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077:
            raise ValueError()
    if root.is_symlink():
        root.unlink()
    if directory is not None and directory.exists():
        shutil.rmtree(directory)
    for target in targets:
        shutil.rmtree(target)
        target.mkdir(mode=0o700)
    receipt['syntheticStatePurged'] = True
    if failed:
        receipt['purgedIncomplete'] = True
    write_json(run / 'receipt.json', receipt)


def warm(run, run_id, args, lock):
    command = [str(HERE.parent / 'run-check'), '--impure', '--file', 'full-check.nix',
               '--argstr', 'runId', run_id]
    for key in ['webArchive', 'cmsArchive', 'tfcSource']:
        command.extend(['--argstr', key, str(Path(getattr(args, key)).resolve(strict=True))])
    if args.documenso:
        command.extend(['--arg', 'withDocumenso', 'true'])
    if args.relay_peers:
        command.extend(['--arg', 'withRelayPeers', 'true'])
    if args.documenso_signing:
        command.extend(['--arg', 'withDocumensoSigning', 'true'])
    if args.all_services:
        command.extend(['--arg', 'withApplications', 'true'])
    command.append('driver')
    child = None
    with (run / 'build.log').open('xb') as log, (run / 'build-output.txt').open('xb') as output:
        try:
            # The driver and guest closures run after the build, outside its temporary roots:
            # root them in the private run directory so a host GC cannot delete them mid-run.
            env = {**os.environ, 'BUILD_LOCK': lock, 'BUILD_LOCK_TIMEOUT': '1200', 'CHECK_TIMEOUT': '1800',
                   'CHECK_BUILD_TIMEOUT': '1200', 'CHECK_STOP_GRACE': '30', 'CHECK_OUT_LINK': str(run / 'driver-root')}
            child = subprocess.Popen(command, cwd=HERE.parent, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=log, start_new_session=True)
            child.wait(timeout=3070)
            if child.returncode != 0:
                raise RuntimeError()
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=65)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
    driver = Path((run / 'build-output.txt').read_text().strip()) / 'bin/nixos-test-driver'
    if not str(driver).startswith(STORE) or not driver.is_file() or (run / 'driver-root').resolve() != driver.parent.parent:
        raise RuntimeError()
    return str(driver)


def execute(args):
    lock = os.environ['BUILD_LOCK']
    if sys.platform != 'linux' or os.geteuid() == 0 or not os.path.isabs(lock):
        raise ValueError()
    parent = Path(args.output).resolve(strict=True)
    if not re.fullmatch(r'/[A-Za-z0-9/_.-]+', str(parent)):
        raise ValueError()
    run, run_id = create_run(parent)
    # Lets a failed-run purge tell that this controller has exited.
    write_json(run / 'controller.json', {'runId': run_id, 'controllerPid': os.getpid(), 'controllerStart': fingerprint(os.getpid())})
    try:
        driver = warm(run, run_id, args, lock)
        containment = contained_driver(run, run_id, [driver, '--no-interactive', '--keep-machine-state', '--output_directory', str(run / 'evidence')], lock, runtime=2700 if args.all_services else 1800, queue=1200)
    finally:
        # Release the run's GC root once its guests are off; the store may then collect it.
        root = run / 'driver-root'
        if root.is_symlink():
            root.unlink()
    scenario = private_json(run / 'evidence' / 'scenario-result.json')
    if scenario.get('scope') != 'terrace-disposable-synthetic' or scenario.get('checks', {}).get('allGuestsStopped') is not True:
        raise RuntimeError()
    for name, directory in [('encrypted-repository.tar', 'retained'), ('recovery-escrow.tar', 'escrow')]:
        path = run / directory / name
        info = path.lstat()
        expected = scenario.get('artifacts', {}).get(name, {})
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size != expected.get('bytes'):
            raise RuntimeError()
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected.get('sha256'):
                raise RuntimeError()
    write_json(run / 'receipt.json', {'schema': 1, 'scope': 'terrace-disposable-synthetic', 'runId': run_id,
                                     'complete': True, 'computeOff': True, 'containment': containment,
                                     'scenario': scenario, 'syntheticStatePurged': False})
    print('Established-service rehearsal complete; all owned compute is off. Private receipt: ' + str(run / 'receipt.json'))


class SafeArguments(argparse.ArgumentParser):
    def error(self, _message):
        # Parser diagnostics can otherwise echo an accidentally supplied secret.
        raise ValueError()


def interrupted(_signum, _frame):
    raise InterruptedError()


def convert_signals():
    # Termination, interrupt and hangup unwind through the cleanup in execute.
    for signum in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(signum, interrupted)


def main():
    os.umask(0o077)
    parser = SafeArguments(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--output', required=True)
    run.add_argument('--documenso', action='store_true', help='Include the optional native Documenso recovery checks.')
    run.add_argument('--documenso-signing', action='store_true', help='Include synthetic signed-envelope recovery; implies --documenso.')
    run.add_argument('--relay-peers', action='store_true', help='Include disposable tailnet clients and private service recovery.')
    run.add_argument('--all-services', action='store_true', help='Include signed documents, private network peers, Windmill and Vaultwarden recovery.')
    for key in ['webArchive', 'cmsArchive', 'tfcSource']:
        run.add_argument('--' + key, required=True)
    delete = sub.add_parser('purge')
    delete.add_argument('run')
    delete.add_argument('--synthetic-only', action='store_true', required=True)
    delete.add_argument('--yes', action='store_true', required=True)
    delete.add_argument('--failed', action='store_true', help='Purge a failed or incomplete run once its compute and controller are gone.')
    args = parser.parse_args()
    if getattr(args, 'all_services', False):
        args.documenso_signing = True
        args.relay_peers = True
    if getattr(args, 'documenso_signing', False):
        args.documenso = True

    convert_signals()
    if args.command == 'purge':
        purge(args.run, args.synthetic_only and args.yes, failed=args.failed)
        print(('Failed or incomplete' if args.failed else 'Completed') + ' synthetic run state purged; receipts, logs and unrelated paths retained.')
    else:
        execute(args)


if __name__ == '__main__':
    try:
        main()
    except Refused as refused:
        print('Purge refused: ' + str(refused) + '. Run state retained.', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Rehearsal rejected or failed; protected run evidence and failure disks retained. No raw diagnostics emitted.', file=sys.stderr)
        sys.exit(1)
