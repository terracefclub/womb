import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

source = Path(__file__).resolve().parents[2] / 'platform/full/concurrent.py'
spec = importlib.util.spec_from_file_location('full_concurrent', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativeWitness(unittest.TestCase):
    def test_exact_child_witnesses_are_private_and_previous_results_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'checks'
            command = {'example': ([sys.executable, '-c', 'print("Synthetic phase passed.")'], 'Synthetic phase passed.')}
            module.run(target, command)
            self.assertEqual(json.loads((target / 'result.json').read_text()), {'example': True})
            self.assertEqual((target / 'result.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                module.run(target, command)

    def test_nonzero_exit_and_wrong_or_unsafe_output_cannot_become_a_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, code in enumerate(['print("Synthetic phase passed.");raise SystemExit(1)', 'print("PRIVATE_CANARY")']):
                target = root / str(index)
                with self.assertRaises(AssertionError):
                    module.run(target, {'example': ([sys.executable, '-c', code], 'Synthetic phase passed.')})
                self.assertFalse((target / 'result.json').exists())
            file = root / 'output';file.write_text('Synthetic phase passed.');file.chmod(0o600)
            link = root / 'link';link.symlink_to(file)
            fifo = root / 'fifo';os.mkfifo(fifo, 0o600)
            for path in [link, fifo]:
                with self.assertRaises((OSError, AssertionError)):
                    module.accepted_output(path, 'Synthetic phase passed.')

    def test_noisy_child_cannot_grow_its_private_output_beyond_the_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'checks'
            with self.assertRaises(AssertionError):
                module.run(target, {'example': ([sys.executable, '-c', 'import sys;sys.stderr.write("x"*(5*1024*1024));print("Synthetic phase passed.")'], 'Synthetic phase passed.')})
            self.assertEqual((target / 'example.stderr').stat().st_size, 4 * 1024 * 1024)
            self.assertFalse((target / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
