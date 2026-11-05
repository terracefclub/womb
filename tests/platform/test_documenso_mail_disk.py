import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

source = Path(__file__).parents[2] / "platform/documenso/inspect-mail-disk.py"
spec = importlib.util.spec_from_file_location("mail_disk", source)
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def header():
    value = bytearray(104)
    value[:4] = b"QFI\xfb"
    struct.pack_into(">I", value, 4, 3)
    struct.pack_into(">Q", value, 24, 12 * disk.GIB)
    return value


class RetainedMailDiskTests(unittest.TestCase):
    def test_flat_bounded_qcow_header_without_external_storage_is_accepted(self):
        self.assertEqual(disk.qcow_header(header()), 12 * disk.GIB)

    def test_backing_encryption_external_data_corruption_and_oversize_rejected(self):
        for offset, format_, value in [(8, ">Q", 512), (16, ">I", 16), (32, ">I", 1),
                                       (72, ">Q", 4), (72, ">Q", 2), (24, ">Q", 17 * disk.GIB)]:
            bad = header()
            struct.pack_into(format_, bad, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                disk.qcow_header(bad)
        for value in [b"", b"not a disk" * 20, header()[:100]]:
            with self.assertRaises(ValueError):
                disk.qcow_header(value)

    def test_nonempty_exact_process_evidence_and_absent_pids_are_required(self):
        rows = [{"pid": 123, "startTicks": 1, "comm": "qemu-system"}, {"pid": 124, "startTicks": 2, "comm": "qemu-system"}]
        self.assertEqual(disk.qemu_records(rows), rows)
        for value in [[], rows[:1], [rows[0], rows[0]], [{**rows[0], "pid": True}, rows[1]]]:
            with self.assertRaises(ValueError):
                disk.qemu_records(value)
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            disk.require_absent(rows, proc)
            (proc / "123").mkdir()
            with self.assertRaises(ValueError):
                disk.require_absent(rows, proc)

    def test_private_receipt_rejects_public_mode_symlink_and_wrong_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt"
            path.write_text("[]")
            path.chmod(0o600)
            self.assertEqual(disk.private_receipt(path, os.getuid()), [])
            with self.assertRaises(ValueError):
                disk.private_receipt(path, os.getuid() + 1)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                disk.private_receipt(path, os.getuid())
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                disk.private_receipt(link, os.getuid())

    def test_hidden_proc_mounts_cannot_make_live_pids_look_absent_after_privilege_drop(self):
        self.assertTrue(disk.visible_proc("rw,nosuid,nodev,noexec"))
        self.assertTrue(disk.visible_proc("rw,hidepid=0"))
        self.assertFalse(disk.visible_proc("rw,hidepid=2"))
        self.assertFalse(disk.visible_proc("rw,hidepid=invisible"))

    def test_parser_stdout_is_discarded_or_file_limited_before_capture(self):
        script = "import os; os.write(1, b'x' * 1000000)"
        self.assertEqual(disk.command([sys.executable, "-c", script], 3, 1024), b"")
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(disk.command([sys.executable, "-c", "print('safe')"], 3, 65536,
                                          stdout_directory=temporary), b"safe\n")
            with self.assertRaises(ValueError):
                disk.command([sys.executable, "-c", script], 3, 65536, stdout_directory=temporary)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_malformed_arguments_have_static_errors_without_private_values(self):
        marker = "synthetic-private-argument"
        result = subprocess.run([sys.executable, str(source), "--source-uid", marker],
                                capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(), "Read-only retained mail inspection failed; private diagnostics suppressed.")


if __name__ == "__main__":
    unittest.main()
