import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('restore_evidence', Path(__file__).with_name('restore-evidence.py'))
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class RestoreEvidenceTests(unittest.TestCase):
    def test_private_exact_copy_missing_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.write_bytes(b'private synthetic diagnostic')
            output = root / 'out'
            evidence.export(output, {'restore.log': str(source), 'missing': str(root / 'missing')})
            self.assertEqual((output / 'restore.log').read_bytes(), source.read_bytes())
            self.assertEqual((output / 'restore.log').stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                evidence.export(output, {})

    def test_symlink_fifo_and_oversize_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            large = root / 'large'
            large.write_bytes(b'x' * (evidence.LIMIT + 1))
            link = root / 'link'
            link.symlink_to(large)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            for index, source in enumerate([large, link, fifo]):
                output = root / ('out-' + str(index))
                with self.assertRaises((ValueError, OSError)):
                    evidence.export(output, {'restore.log': str(source)})
                self.assertFalse((output / 'restore.log').exists())


if __name__ == '__main__':
    unittest.main()
