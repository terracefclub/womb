import importlib.util
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

path = Path(__file__).resolve().parents[2] / 'platform/full/pg-sample.py'
spec = importlib.util.spec_from_file_location('pg_sample', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ConnectionEvidence(unittest.TestCase):
    def test_only_bounded_numeric_counts_are_accepted(self):
        self.assertEqual(module.parse_sample(b'64|19\n'), (64, 19))
        for invalid in [b'', b'64|65', b'0|0', b'1025|0', b'64|1\nPRIVATE', b'64|-1', b'64|1.2']:
            with self.assertRaisesRegex(ValueError, '^Connection sample rejected[.]$'):
                module.parse_sample(invalid)

    def test_private_atomic_result_and_existing_staging_file_rejection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'DIRECTORY', Path(directory)):
            module.store({'samples': 1, 'sampledPeakClients': 19, 'configuredMaximum': 64})
            target = Path(directory) / 'result.json'
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse((Path(directory) / 'next.json').exists())
            (Path(directory) / 'next.json').symlink_to(target)
            with self.assertRaises(FileExistsError):
                module.store({'samples': 2})
            self.assertIn('19', target.read_text())

    def test_group_stop_accepts_cancelled_query_only_after_real_sample_and_signal(self):
        handlers = {}
        calls = [0]

        def run(*_args, **_kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return subprocess.CompletedProcess([], 0, b'64|19\n')
            handlers[module.signal.SIGTERM](module.signal.SIGTERM, None)
            raise subprocess.CalledProcessError(-15, ['psql'])

        with patch.object(module.signal, 'signal', side_effect=lambda key, value: handlers.__setitem__(key, value)), \
                patch.object(module.time, 'monotonic', return_value=0), patch.object(module.time, 'sleep'), \
                patch.object(module.subprocess, 'run', side_effect=run), patch.object(module, 'store') as store:
            module.main()
            self.assertEqual(store.call_count, 1)
            self.assertEqual(calls[0], 2)
        with patch.object(module.signal, 'signal'), patch.object(module.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['psql'])):
            with self.assertRaises(subprocess.CalledProcessError):
                module.main()


if __name__ == '__main__':
    unittest.main()
