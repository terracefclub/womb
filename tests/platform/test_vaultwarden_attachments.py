import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).parents[2] / 'platform/vaultwarden/attachment-files.py'
spec = importlib.util.spec_from_file_location('vault_attachments', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class EncryptedFiles(unittest.TestCase):
    def fixtures(self, root):
        paths=[]
        for digit in ['1','2']:
            directory=root/(digit*8+'-'+digit*4+'-'+digit*4+'-'+digit*4+'-'+digit*12)
            directory.mkdir()
            path=directory/(digit*20)
            path.write_bytes(b'\x00synthetic ciphertext '+digit.encode())
            paths.append(path)
        return paths

    def test_hashes_detect_changes_and_plaintext_or_extra_entries_reject(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=self.fixtures(root)
            original=module.inventory(root)
            paths[0].write_bytes(b'\x01changed ciphertext')
            self.assertNotEqual(module.inventory(root),original)
            paths[0].write_bytes(b'Synthetic encrypted attachment bytes; plaintext is forbidden here')
            with self.assertRaises(AssertionError):
                module.inventory(root)
            paths[0].write_bytes(b'x'*4097)
            with self.assertRaises(AssertionError):
                module.inventory(root)
            paths[0].write_bytes(b'ciphertext')
            (root/('3'*8+'-'+'3'*4+'-'+'3'*4+'-'+'3'*4+'-'+'3'*12)).mkdir()
            with self.assertRaises(AssertionError):
                module.inventory(root)

    def test_symlink_and_fifo_are_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=self.fixtures(root)
            paths[0].unlink();paths[0].symlink_to(paths[1])
            with self.assertRaises(OSError):
                module.inventory(root)
            paths[0].unlink();os.mkfifo(paths[0],0o600)
            code='import importlib.util,sys;from pathlib import Path;spec=importlib.util.spec_from_file_location("a",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\ntry:m.inventory(Path(sys.argv[2]))\nexcept AssertionError:sys.exit(0)\nsys.exit(2)'
            result=subprocess.run([sys.executable,'-c',code,str(SOURCE),str(root)],capture_output=True,timeout=2)
            self.assertEqual(result.returncode,0)
            self.assertEqual(result.stdout,b'')
            self.assertEqual(result.stderr,b'')

    def test_cli_errors_never_print_supplied_values(self):
        result=subprocess.run([sys.executable,str(SOURCE),'private-canary-never-print'],capture_output=True,text=True,timeout=2)
        self.assertEqual(result.returncode,1)
        self.assertEqual(result.stdout,'')
        self.assertEqual(result.stderr,'Vaultwarden attachment inventory rejected; diagnostics suppressed.\n')


if __name__ == '__main__':
    unittest.main()
