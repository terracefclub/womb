import json
import argparse
import ast
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


if __name__ == '__main__':
    unittest.main()
