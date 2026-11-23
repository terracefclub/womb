import json
import argparse
import ast
import contextlib
import io
import hashlib
import signal
import stat
import os
from pathlib import Path
import tempfile
import subprocess
import shutil
import sys
import unittest
from unittest.mock import patch

import full
from host import create_run, write_json


class CgroupInspection(unittest.TestCase):
    script = Path(__file__).resolve().parents[1] / 'full' / 'empty-cgroups.sh'

    def check(self, find_script, cat_script=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'kubepods.slice'
            fixture.mkdir()
            (fixture / 'cgroup.procs').write_text('')
            for name, content in [('find', find_script), ('cat', cat_script)]:
                if content is not None:
                    path = root / name
                    path.write_text('#!/bin/bash\n' + content + '\n')
                    path.chmod(0o700)
            return subprocess.run(['bash', str(self.script)], capture_output=True, text=True, timeout=5,
                                  env={**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                                       'REAL_FIND': shutil.which('find'), 'CGROUP_FIXTURE': str(root)})

    def test_successful_empty_inspection_accepts_but_live_writer_refuses(self):
        self.assertEqual(self.check('exit 0').returncode, 0)
        result = self.check("printf '1234\\n'")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_find_error_with_empty_stdout_refuses_without_private_diagnostics(self):
        result = self.check("echo 'PRIVATE_FIND_MARKER' >&2; exit 1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Cgroup inspection failed.\n')

    def test_batched_cat_error_propagates_through_real_find(self):
        result = self.check('exec "$REAL_FIND" "$CGROUP_FIXTURE" "${@:2}"',
                            "echo 'PRIVATE_CAT_MARKER' >&2; exit 1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Cgroup inspection failed.\n')


class PrivateEvidence(unittest.TestCase):
    def test_bounded_cross_directory_copy_rejects_unsafe_sources_and_overwrites(self):
        source = Path(__file__).resolve().parents[1] / 'full' / 'scenario.py'
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'retain_private_artifact')
        namespace = {'os': os, 'stat': stat}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        retain = namespace['retain_private_artifact']
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'durable').mkdir()
            good = root / 'artifact'; good.write_bytes(b'synthetic private bytes'); good.chmod(0o600)
            target = root / 'durable' / 'artifact'
            retain(good, target, 64)
            self.assertEqual(target.read_bytes(), good.read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): retain(good, target, 64)
            link = root / 'link'; link.symlink_to(good)
            fifo = root / 'fifo'; os.mkfifo(fifo, 0o600)
            for unsafe in [link, fifo]:
                with self.assertRaises((OSError, AssertionError)):
                    retain(unsafe, root / 'rejected', 64)
            with self.assertRaises(AssertionError): retain(good, root / 'rejected', 2)
            good.chmod(0o644)
            with self.assertRaises(AssertionError): retain(good, root / 'rejected', 64)
            self.assertFalse((root / 'rejected').exists())


class CliComposition(unittest.TestCase):
    def test_signing_is_explicit_and_implies_the_documenso_backup_scope(self):
        required = ['--output', '/synthetic/private', '--webArchive', '/synthetic/web',
                    '--cmsArchive', '/synthetic/cms', '--tfcSource', '/synthetic/source']
        for flags, expected in [([], (False, False)), (['--documenso'], (True, False)),
                                (['--documenso-signing'], (True, True))]:
            with patch.object(full.sys, 'argv', ['full-rehearsal', 'run', *required, *flags]), \
                    patch.object(full.signal, 'signal'), patch.object(full, 'execute') as execute:
                full.main()
            args = execute.call_args.args[0]
            self.assertEqual((args.documenso, args.documenso_signing), expected)
            self.assertFalse(args.relay_peers)
            self.assertFalse(args.all_services)

    def test_all_services_includes_every_persistent_application_and_private_network(self):
        required = ['--output', '/synthetic/private', '--webArchive', '/synthetic/web',
                    '--cmsArchive', '/synthetic/cms', '--tfcSource', '/synthetic/source']
        with patch.object(full.sys, 'argv', ['full-rehearsal', 'run', *required, '--all-services']), \
                patch.object(full.signal, 'signal'), patch.object(full, 'execute') as execute:
            full.main()
        args = execute.call_args.args[0]
        self.assertTrue(args.all_services and args.documenso and args.documenso_signing and args.relay_peers)

class DriverRoot(unittest.TestCase):
    """The driver's GC root: created before guests start, released on every exit."""

    def setUp(self):
        self.parent = tempfile.TemporaryDirectory()
        self.addCleanup(self.parent.cleanup)
        self.base = Path(self.parent.name).resolve()
        self.base.chmod(0o700)
        self.store = self.base / 'store'
        self.output = self.store / 'driver-output'
        (self.output / 'bin').mkdir(parents=True)
        self.driver = self.output / 'bin' / 'nixos-test-driver'
        self.driver.write_text('#!/bin/sh\n')
        for name in ['web', 'cms', 'tfc']:
            (self.base / name).write_text('synthetic')
        self.args = argparse.Namespace(webArchive=str(self.base / 'web'), cmsArchive=str(self.base / 'cms'),
                                       tfcSource=str(self.base / 'tfc'), documenso=False, relay_peers=False,
                                       documenso_signing=False, all_services=False)
        self.out = self.base / 'out'
        self.out.mkdir(mode=0o700)
        patcher = patch.object(full, 'STORE', str(self.store) + os.sep)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.run, self.run_id = create_run(self.base)

    def popen(self, link_to='output'):
        test, calls = self, []

        class Child:
            returncode = 0

            def __init__(self, command, env, stdout, **_kwargs):
                calls.append(env)
                stdout.write(str(test.output).encode())
                if link_to == 'output':
                    os.symlink(test.output, env['CHECK_OUT_LINK'])
                elif link_to:
                    os.symlink(link_to, env['CHECK_OUT_LINK'])

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        return patch.object(full.subprocess, 'Popen', Child), calls

    def test_warm_roots_the_driver_in_the_run_directory_and_checks_it_resolves(self):
        popen, calls = self.popen()
        with popen:
            self.assertEqual(full.warm(self.run, self.run_id, self.args, '/synthetic/lock'), str(self.driver))
        self.assertEqual(calls[0]['CHECK_OUT_LINK'], str(self.run / 'driver-root'))
        self.assertTrue((self.run / 'driver-root').is_symlink())
        self.assertEqual((self.run / 'driver-root').resolve(), self.output)

    def test_warm_refuses_a_root_that_is_missing_or_resolves_elsewhere(self):
        elsewhere = self.base / 'elsewhere'
        elsewhere.mkdir()
        for link_to in (None, elsewhere):
            run, run_id = create_run(self.base)
            popen, _ = self.popen(link_to)
            with self.subTest(root=link_to), popen, self.assertRaises(RuntimeError):
                full.warm(run, run_id, self.args, '/synthetic/lock')

    def execute(self, warm_roots=True, containment=None):
        """Run execute with a fake build and containment; return the run directory."""
        def warm(run, run_id, _args, _lock):
            os.symlink(self.output, run / 'driver-root')
            if not warm_roots:
                raise RuntimeError()
            return str(self.driver)

        def contained(run, run_id, command, _lock, **_kwargs):
            # Guests would start here: the root must already resolve to the driver output.
            self.assertEqual((run / 'driver-root').resolve(), self.output)
            self.assertEqual(command[0], str(self.driver))
            return containment(run, run_id) if containment else self.succeed(run)

        with patch.object(full.sys, 'platform', 'linux'), patch.object(full.os, 'geteuid', return_value=1000), \
                patch.dict(os.environ, {'BUILD_LOCK': '/synthetic/lock'}), \
                patch.object(full, 'warm', warm), patch.object(full, 'contained_driver', contained):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    full.execute(argparse.Namespace(**{**vars(self.args), "output": str(self.out)}))
            finally:
                [run] = self.out.glob('run-*')
                self.last = run
        return self.last

    def succeed(self, run):
        artifacts = {}
        for name, directory in [('encrypted-repository.tar', 'retained'), ('recovery-escrow.tar', 'escrow')]:
            data = name.encode()
            path = run / directory / name
            path.write_bytes(data)
            path.chmod(0o600)
            artifacts[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        write_json(run / 'evidence' / 'scenario-result.json', {
            'scope': 'terrace-disposable-synthetic', 'checks': {'allGuestsStopped': True}, 'artifacts': artifacts})
        return {}

    def test_root_is_released_after_success_and_the_receipt_is_complete(self):
        run = self.execute()
        self.assertFalse(os.path.lexists(run / 'driver-root'))
        self.assertTrue(self.output.is_dir())
        receipt = json.loads((run / 'receipt.json').read_text())
        self.assertTrue(receipt['complete'] and receipt['computeOff'])

    def test_root_is_released_after_a_failed_build_or_failed_guests(self):
        with self.assertRaises(RuntimeError):
            self.execute(warm_roots=False)
        self.assertFalse(os.path.lexists(self.last / 'driver-root'))
        self.out = self.base / 'out2'
        self.out.mkdir(mode=0o700)

        def fail(_run, _run_id):
            raise RuntimeError()

        with self.assertRaises(RuntimeError):
            self.execute(containment=fail)
        self.assertFalse(os.path.lexists(self.last / 'driver-root'))
        self.assertFalse(json.loads((self.last / 'receipt.json').read_text())['complete'])
        self.assertTrue(self.output.is_dir())

    def test_a_failed_run_records_its_controller_for_the_failed_run_purge(self):
        def fail(_run, _run_id):
            raise RuntimeError()

        with patch.object(full, 'fingerprint', return_value='controller-start'), self.assertRaises(RuntimeError):
            self.execute(containment=fail)
        record = json.loads((self.last / 'controller.json').read_text())
        self.assertEqual(record, {'runId': self.last.name.removeprefix('run-'), 'controllerPid': os.getpid(), 'controllerStart': 'controller-start'})
        self.assertEqual((self.last / 'controller.json').stat().st_mode & 0o777, 0o600)

    def test_root_is_released_when_a_signal_interrupts_the_guests(self):
        for number, signum in enumerate([signal.SIGTERM, signal.SIGINT, signal.SIGHUP]):
            self.out = self.base / f'signal{number}'
            self.out.mkdir(mode=0o700)

            def interrupt(_run, _run_id, signum=signum):
                os.kill(os.getpid(), signum)
                for _ in range(100):
                    __import__('time').sleep(0.05)
                raise AssertionError('signal was not converted to an exception')

            previous = {name: signal.getsignal(name) for name in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]}
            full.convert_signals()
            try:
                with self.subTest(signal=signal.Signals(signum).name), self.assertRaises(InterruptedError):
                    self.execute(containment=interrupt)
            finally:
                for name, handler in previous.items():
                    signal.signal(name, handler)
            self.assertFalse(os.path.lexists(self.last / 'driver-root'))
            self.assertFalse(json.loads((self.last / 'receipt.json').read_text())['complete'])


class SyntheticPurge(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.TemporaryDirectory()
        self.addCleanup(self.parent.cleanup)
        self.base = Path(self.parent.name)
        self.base.chmod(0o700)
        self.run, self.run_id = create_run(self.base)
        self.runtime = Path(tempfile.mkdtemp(prefix='tr-' + self.run_id[:8] + '-', dir='/tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.runtime, ignore_errors=True))
        self.runtime.chmod(0o700)
        write_json(self.runtime / 'owner.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id})
        write_json(self.run / 'runtime.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'directory': str(self.runtime)})
        write_json(self.run / 'receipt.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'complete': True, 'computeOff': True})
        self.sentinel = self.base / 'unrelated'
        self.sentinel.write_text('retained')
        for name in ['state', 'retained', 'escrow']:
            (self.run / name / 'synthetic').write_text('disposable')
        self.off = patch.object(full, 'unit_off', return_value=True)
        self.guests = patch.object(full, 'owned_guests', return_value={})
        self.off.start()
        self.guests.start()
        self.addCleanup(self.off.stop)
        self.addCleanup(self.guests.stop)

    def assert_preserved(self):
        self.assertEqual(self.sentinel.read_text(), 'retained')
        self.assertTrue((self.run / 'retained' / 'synthetic').exists())

    def test_completed_run_only_deletes_scoped_synthetic_state(self):
        full.purge(self.run, True)
        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.sentinel.read_text(), 'retained')
        for name in ['state', 'retained', 'escrow']:
            self.assertEqual(list((self.run / name).iterdir()), [])
        self.assertTrue(json.loads((self.run / 'receipt.json').read_text())['syntheticStatePurged'])
        self.assertTrue((self.run / 'evidence').is_dir())

    def test_unsafe_disk_output_path_rejected_before_any_job(self):
        parent = self.base / 'unsafe,output'
        parent.mkdir(mode=0o700)
        with patch.object(full.sys, 'platform', 'linux'), patch.object(full.os, 'geteuid', return_value=1000), patch.dict(os.environ, {'BUILD_LOCK': '/tmp/synthetic-lock'}), patch.object(full, 'warm') as warm:
            with self.assertRaises(ValueError):
                full.execute(argparse.Namespace(output=str(parent)))
            warm.assert_not_called()
        self.assertEqual(list(parent.iterdir()), [])
        self.assert_preserved()

    def test_invalid_cli_arguments_do_not_echo_values(self):
        marker = 'synthetic-private-argument-marker'
        result = subprocess.run([sys.executable, str(Path(full.__file__)), 'purge', str(self.run), '--unknown', marker],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assert_preserved()

    def test_explicit_confirmation_required(self):
        with self.assertRaises(ValueError):
            full.purge(self.run)
        self.assert_preserved()

    def test_incomplete_or_uncertain_compute_preserved(self):
        for complete, compute_off in [(False, True), (True, False)]:
            write_json(self.run / 'receipt.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'complete': complete, 'computeOff': compute_off})
            with self.assertRaises(ValueError):
                full.purge(self.run, True)
            self.assert_preserved()
        write_json(self.run / 'receipt.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'complete': True, 'computeOff': True})
        with patch.object(full, 'unit_off', return_value=False), self.assertRaises(ValueError):
            full.purge(self.run, True)
        with patch.object(full, 'owned_guests', return_value={123: 'synthetic'}), self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assert_preserved()

    def test_foreign_scope_or_runtime_marker_preserved(self):
        write_json(self.runtime / 'owner.json', {'scope': 'terrace-disposable-synthetic', 'runId': '0' * 32})
        with self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assert_preserved()

    def test_symlink_root_cannot_redirect_deletion(self):
        import shutil
        shutil.rmtree(self.run / 'escrow')
        (self.run / 'escrow').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assertTrue(self.runtime.exists())
        self.assert_preserved()

    def test_fifo_input_rejected_without_blocking(self):
        (self.runtime / 'owner.json').unlink()
        os.mkfifo(self.runtime / 'owner.json', 0o600)
        with self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assert_preserved()

    def test_leftover_driver_root_is_removed_but_never_followed(self):
        target = self.base / 'rooted-output'
        target.mkdir()
        (target / 'keep').write_text('retained')
        (self.run / 'driver-root').symlink_to(target, target_is_directory=True)
        full.purge(self.run, True)
        self.assertFalse(os.path.lexists(self.run / 'driver-root'))
        self.assertEqual((target / 'keep').read_text(), 'retained')
        self.assertEqual(self.sentinel.read_text(), 'retained')
        self.assertTrue(json.loads((self.run / 'receipt.json').read_text())['syntheticStatePurged'])

    def test_driver_root_that_is_not_a_symlink_blocks_the_whole_purge(self):
        for make in (lambda path: path.mkdir(), lambda path: path.write_text('not a link')):
            path = self.run / 'driver-root'
            make(path)
            with self.assertRaises(ValueError):
                full.purge(self.run, True)
            self.assert_preserved()
            self.assertTrue(self.runtime.exists())
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()

    def test_public_or_symlink_receipt_rejected(self):
        (self.run / 'receipt.json').chmod(0o644)
        with self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assert_preserved()
        (self.run / 'receipt.json').unlink()
        (self.run / 'receipt.json').symlink_to(self.sentinel)
        with self.assertRaises(OSError):
            full.purge(self.run, True)
        self.assert_preserved()



class FailedRunPurge(unittest.TestCase):
    """purge --failed: a failed or incomplete run, once its compute and its controller are gone."""

    assert_preserved = SyntheticPurge.assert_preserved

    def setUp(self):
        SyntheticPurge.setUp(self)
        write_json(self.run / 'receipt.json', {'schema': 1, 'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'complete': False, 'computeOff': False})
        self.controller(self.run_id, 4242, 'start-of-the-exited-controller')
        (self.run / 'driver.log').write_text('kept')
        (self.run / 'evidence' / 'concurrent-failure-restored').mkdir(mode=0o700)
        (self.run / 'evidence' / 'concurrent-failure-restored' / 'documents.stderr').write_text('kept')
        # Whatever process 4242 is now, it is not the controller that started this run.
        self.alive = patch.object(full, 'fingerprint', return_value='start-of-another-process')
        self.alive.start()
        self.addCleanup(self.alive.stop)

    def controller(self, run_id, pid, start, name='controller.json'):
        write_json(self.run / name, {'runId': run_id, 'controllerPid': pid, 'controllerStart': start})

    def test_failed_run_purge_removes_disks_repository_and_escrow_and_keeps_logs(self):
        full.purge(self.run, True, failed=True)
        self.assertFalse(self.runtime.exists())
        for name in ['state', 'retained', 'escrow']:
            self.assertEqual(list((self.run / name).iterdir()), [])
        self.assertEqual((self.run / 'driver.log').read_text(), 'kept')
        self.assertEqual((self.run / 'evidence' / 'concurrent-failure-restored' / 'documents.stderr').read_text(), 'kept')
        receipt = json.loads((self.run / 'receipt.json').read_text())
        self.assertEqual((receipt['complete'], receipt['syntheticStatePurged'], receipt['purgedIncomplete']), (False, True, True))
        self.assertEqual(self.sentinel.read_text(), 'retained')

    def test_run_that_failed_before_its_guardian_started(self):
        # No runtime record: nothing in /tmp is touched, not even a directory that looks like the run's.
        (self.run / 'runtime.json').unlink()
        full.purge(self.run, True, failed=True)
        self.assertTrue(self.runtime.exists())
        self.assertEqual(list((self.run / 'state').iterdir()), [])

    def test_driver_phase_record_alone_is_accepted(self):
        # Runs started before controller.json existed: the driver phase's control.json identifies the controller.
        (self.run / 'controller.json').unlink()
        self.controller(self.run_id, 4242, 'start-of-the-exited-controller', 'control.json')
        full.purge(self.run, True, failed=True)
        self.assertEqual(list((self.run / 'retained').iterdir()), [])

    def test_failed_mode_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            full.purge(self.run, failed=True)
        self.assert_preserved()

    def test_complete_run_is_refused_by_failed_mode_and_failed_run_by_ordinary_mode(self):
        with self.assertRaises(ValueError):
            full.purge(self.run, True)
        self.assert_preserved()
        write_json(self.run / 'receipt.json', {'scope': 'terrace-disposable-synthetic', 'runId': self.run_id, 'complete': True, 'computeOff': True})
        with self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()

    def test_live_or_unknown_controller_preserves_everything(self):
        with patch.object(full, 'fingerprint', return_value='start-of-the-exited-controller'), self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()
        for name, record in [('controller.json', {'runId': '0' * 32, 'controllerPid': 4242, 'controllerStart': 'x'}),
                             ('controller.json', {'runId': self.run_id, 'controllerPid': '4242', 'controllerStart': 'x'}),
                             ('controller.json', {'runId': self.run_id, 'controllerPid': 4242, 'controllerStart': ''})]:
            write_json(self.run / name, record)
            with self.subTest(record=record), self.assertRaises(ValueError):
                full.purge(self.run, True, failed=True)
            self.assert_preserved()
        (self.run / 'controller.json').unlink()
        with self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()

    def refused(self, pattern):
        with self.assertRaisesRegex(full.Refused, pattern):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()
        self.assertTrue(self.runtime.exists())

    def test_every_controller_record_must_be_gone(self):
        # Both records exist; either one naming a live process refuses (not only the first one read).
        starts = {4242: 'start-of-another-process', 5151: 'start-of-the-driver-controller'}
        self.controller(self.run_id, 5151, 'start-of-the-driver-controller', 'control.json')
        with patch.object(full, 'fingerprint', side_effect=lambda pid: starts[pid]):
            self.refused(r'^the controller in control\.json is still running$')
        starts = {4242: 'start-of-the-exited-controller', 5151: 'start-of-another-process'}
        with patch.object(full, 'fingerprint', side_effect=lambda pid: starts[pid]):
            self.refused(r'^the controller in controller\.json is still running$')

    def test_symlinked_or_non_object_controller_record_is_refused(self):
        target = self.base / 'elsewhere.json'
        write_json(target, {'runId': self.run_id, 'controllerPid': 4242, 'controllerStart': 'start-of-the-exited-controller'})
        (self.run / 'controller.json').unlink()
        (self.run / 'controller.json').symlink_to(target)
        self.refused(r'^malformed controller record controller\.json$')
        (self.run / 'controller.json').unlink()
        for value in [[self.run_id, 4242], 4242, 'controller', None]:
            write_json(self.run / 'controller.json', value)
            with self.subTest(value=value):
                self.refused(r'^malformed controller record controller\.json$')

    def test_a_controller_that_exits_while_read_is_gone_but_an_unreadable_one_refuses(self):
        # ProcessLookupError: no process has the id any more, so it cannot be the controller.
        with patch.object(full, 'fingerprint', side_effect=ProcessLookupError()):
            full.purge(self.run, True, failed=True)
        self.assertEqual(list((self.run / 'state').iterdir()), [])

    def test_unreadable_process_state_refuses(self):
        with patch.object(full, 'fingerprint', side_effect=PermissionError()):
            self.refused(r'^cannot tell whether the controller in controller\.json is running$')

    def test_an_unexpected_process_state_line_refuses(self):
        # The real fingerprint() on a /proc/PID/stat line without its fields raises IndexError: a refusal, not a crash.
        import host
        real = host.Path.read_text
        for line in ['4242 (python3) S 1', '4242 python3 S 1 2 3', '']:
            def read_text(path, *args, **kwargs):
                return line if str(path) == '/proc/4242/stat' else real(path, *args, **kwargs)
            with self.subTest(line=line), patch.object(full, 'fingerprint', host.fingerprint), \
                    patch.object(host.Path, 'read_text', read_text):
                self.refused(r'^cannot tell whether the controller in controller\.json is running$')

    def test_a_controller_record_too_deep_to_read_refuses(self):
        # Reading a deeply nested record raises RecursionError on Pythons whose JSON reader counts recursion
        # (3.9 at these depths; 3.14 parses them): a refusal, not a crash, whichever Python runs the tool.
        real = full.private_json
        def too_deep(path, *args, **kwargs):
            if Path(path).name == 'controller.json':
                raise RecursionError('maximum recursion depth exceeded while decoding a JSON array')
            return real(path, *args, **kwargs)
        with patch.object(full, 'private_json', too_deep):
            self.refused(r'^malformed controller record controller\.json$')
        # The same with real records within the 4096-byte limit.
        for text in ['[' * 2040 + ']' * 2040, '{"a":' * 680 + '1' + '}' * 680]:
            self.assertLessEqual(len(text), 4096)
            (self.run / 'controller.json').unlink()
            fd = os.open(self.run / 'controller.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
            with self.subTest(depth=text[:2]):
                self.refused(r'^malformed controller record controller\.json$')

    def test_running_guests_or_unit_preserve_everything(self):
        with patch.object(full, 'unit_off', return_value=False), self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        with patch.object(full, 'owned_guests', return_value={123: 'synthetic'}), self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()
        self.assertTrue(self.runtime.exists())

    def test_public_controller_record_or_symlinked_run_refused(self):
        (self.run / 'controller.json').chmod(0o644)
        with self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()
        (self.run / 'controller.json').chmod(0o600)
        link = self.base / 'link-to-run'
        link.symlink_to(self.run, target_is_directory=True)
        with self.assertRaises(ValueError):
            full.purge(link, True, failed=True)
        self.assert_preserved()

    def test_failed_mode_never_follows_a_symlink_out_of_the_run(self):
        shutil.rmtree(self.run / 'state')
        (self.run / 'state').symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(ValueError):
            full.purge(self.run, True, failed=True)
        self.assert_preserved()
        self.assertTrue(self.runtime.exists())

    def test_failed_flag_still_needs_synthetic_only_and_yes(self):
        for extra in [['--yes', '--failed'], ['--synthetic-only', '--failed']]:
            result = subprocess.run([sys.executable, str(Path(full.__file__)), 'purge', str(self.run), *extra],
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
