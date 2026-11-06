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

from host import HERE, contained_driver, create_run, owned_guests, unit_off, write_json


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


def purge(path, confirmed=False):
    if not confirmed:
        raise ValueError()
    run, run_id, receipt = checked_run(path)
    unit = 'terrace-rehearsal-' + run_id + '.service'
    # Failure disks stay protected until separately captured and investigated.
    # This command purges only a completed, verified synthetic rehearsal.
    if receipt.get('complete') is not True or receipt.get('computeOff') is not True or not unit_off(unit) or owned_guests(run_id):
        raise ValueError()
    record = private_json(run / 'runtime.json', 4096)
    directory = Path(record.get('directory', ''))
    if record.get('scope') != receipt['scope'] or record.get('runId') != run_id:
        raise ValueError()
    if directory.parent != Path('/tmp') or not directory.name.startswith('tr-' + run_id[:8] + '-') or directory.is_symlink():
        raise ValueError()
    if directory.exists():
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError()
        if private_json(directory / 'owner.json', 4096) != {'scope': receipt['scope'], 'runId': run_id}:
            raise ValueError()
    # Validate the whole deletion set BEFORE mutating anything. Symlink roots
    # cannot redirect traversal. Only the run's dedicated directories are owned.
    targets = [run / name for name in ['state', 'retained', 'escrow']]
    for target in targets:
        if target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077:
            raise ValueError()
    if directory.exists():
        shutil.rmtree(directory)
    for target in targets:
        shutil.rmtree(target)
        target.mkdir(mode=0o700)
    receipt['syntheticStatePurged'] = True
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
            env = {**os.environ, 'BUILD_LOCK': lock, 'BUILD_LOCK_TIMEOUT': '1200', 'CHECK_TIMEOUT': '1800',
                   'CHECK_BUILD_TIMEOUT': '1200', 'CHECK_STOP_GRACE': '30'}
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
    if not str(driver).startswith('/nix/store/') or not driver.is_file():
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
    driver = warm(run, run_id, args, lock)
    containment = contained_driver(run, run_id, [driver, '--no-interactive', '--keep-machine-state', '--output_directory', str(run / 'evidence')], lock, runtime=2700 if args.all_services else 1800, queue=1200)
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
    args = parser.parse_args()
    if getattr(args, 'all_services', False):
        args.documenso_signing = True
        args.relay_peers = True
    if getattr(args, 'documenso_signing', False):
        args.documenso = True

    def interrupted(_signum, _frame):
        raise InterruptedError()

    for signum in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(signum, interrupted)
    if args.command == 'purge':
        purge(args.run, args.synthetic_only and args.yes)
        print('Completed synthetic run state purged; receipts and unrelated paths retained.')
    else:
        execute(args)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Rehearsal rejected or failed; protected run evidence and failure disks retained. No raw diagnostics emitted.', file=sys.stderr)
        sys.exit(1)
