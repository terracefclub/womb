import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import rehearsal_escrow as escrow
import rehearsal_lease as lease


class EscrowTest(unittest.TestCase):
    def test_private_material_does_not_replace_existing(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            destination = root / "receipt"
            escrow.put_bytes(destination, b"synthetic")
            self.assertEqual(lease.read_private(destination, 10), b"synthetic")
            with self.assertRaises(FileExistsError):
                escrow.put_bytes(destination, b"new")
            self.assertEqual(lease.read_private(destination, 10), b"synthetic")

    def test_only_exact_unix_scope_and_inactive_service_can_stop(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            manifest = {"run": "a" * 32}
            name, socket, _ = escrow.scope(root, manifest)
            with (patch.object(escrow.timer, "system") as system,
                  patch.object(escrow.timer, "CGROUP", root),
                  patch.object(escrow.timer, "read_properties", return_value={"ActiveState": "inactive"})):
                escrow.stop(root, manifest)
                self.assertEqual(system.call_args.args[-1], name)
                socket.touch()
                with self.assertRaises(lease.Rejected):
                    escrow.stop(root, manifest)
                socket.unlink()
                (root / name).mkdir()
                with self.assertRaises(lease.Rejected):
                    escrow.stop(root, manifest)


if __name__ == "__main__":
    unittest.main()
