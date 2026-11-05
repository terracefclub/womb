import importlib.util
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

source = Path(__file__).parents[2] / "platform/full/inspect-restore-disk.py"
spec = importlib.util.spec_from_file_location("restore_disk", source)
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def header(size=24 * disk.GIB):
    value = bytearray(104)
    value[:4] = b"QFI\xfb"
    struct.pack_into(">I", value, 4, 3)
    struct.pack_into(">Q", value, 24, size)
    return value


def processes():
    return [{"pid": 123, "start": "12", "origins": ["owned-test"]},
            {"pid": 124, "start": "13", "origins": ["owned-test"]}]


class RetainedRestoreDiskTests(unittest.TestCase):
    def test_24_gib_and_boundary_32_gib_flat_images_are_supported(self):
        self.assertEqual(disk.qcow_header(header()), 24 * disk.GIB)
        self.assertEqual(disk.qcow_header(header(32 * disk.GIB)), 32 * disk.GIB)

    def test_backing_encryption_external_data_corruption_and_oversize_rejected(self):
        for offset, format_, value in [(8, ">Q", 512), (16, ">I", 16), (32, ">I", 1),
                                       (72, ">Q", 4), (72, ">Q", 2), (24, ">Q", 32 * disk.GIB + 1)]:
            bad = header()
            struct.pack_into(format_, bad, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                disk.qcow_header(bad)
        for value in [b"", b"not a disk" * 20, header()[:100]]:
            with self.assertRaises(ValueError):
                disk.qcow_header(value)

    def test_exact_unique_process_count_and_identity_shape_required(self):
        rows = processes()
        self.assertEqual(disk.process_records(rows, 2), [{"pid": 123, "start": "12"}, {"pid": 124, "start": "13"}])
        for value, count in [([], 0), (rows, 1), ([rows[0], rows[0]], 2),
                             ([{**rows[0], "pid": True}], 1), ([{**rows[0], "start": 12}], 1),
                             ([{**rows[0], "start": "0"}], 1), ([{**rows[0], "origins": []}], 1)]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                disk.process_records(value, count)

    def test_any_live_or_reused_pid_prevents_inspection(self):
        rows = disk.process_records(processes(), 2)
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            disk.support.require_absent(rows, proc)
            (proc / "124").mkdir()
            with self.assertRaises(ValueError):
                disk.support.require_absent(rows, proc)

    def test_source_requires_exact_owner_private_regular_nonempty_file(self):
        good = dict(st_mode=stat.S_IFREG | 0o600, st_uid=1001, st_size=57933824)
        disk.source_info(SimpleNamespace(**good), 1001)
        for key, value in [("st_uid", 1002), ("st_mode", stat.S_IFREG | 0o644),
                           ("st_mode", stat.S_IFIFO | 0o600), ("st_size", 0), ("st_size", 8 * disk.GIB + 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                disk.source_info(SimpleNamespace(**{**good, key: value}), 1001)

    def test_receipt_symlink_fifo_public_and_wrong_owner_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt"
            path.write_text("[]")
            path.chmod(0o600)
            self.assertEqual(disk.support.private_receipt(path, os.getuid()), [])
            with self.assertRaises(ValueError):
                disk.support.private_receipt(path, os.getuid() + 1)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                disk.support.private_receipt(path, os.getuid())
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                disk.support.private_receipt(link, os.getuid())
            fifo = Path(temporary) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ValueError):
                disk.support.private_receipt(fifo, os.getuid())

    def test_only_eight_fixed_restore_files_and_five_known_databases(self):
        self.assertEqual(len(disk.FILES), 8)
        self.assertEqual(len({name for name, _ in disk.FILES}), 8)
        self.assertEqual(disk.DATABASES, ("terrace_cms", "keycloak", "terrace_documenso", "terrace_windmill", "terrace_vaultwarden"))
        for name, path in disk.FILES:
            self.assertNotIn("*", path)
            self.assertNotIn("/", name)
            self.assertTrue(path == "/var/tmp/rehearsal-restore.log" or path.startswith("/var/lib/rehearsal-recovery/"))

    def test_parser_stdout_cannot_allocate_an_unbounded_pipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                disk.support.command([sys.executable, "-c", "import os; os.write(1,b'x'*1000000)"], 3, 65536, stdout_directory=temporary)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_private_arguments_never_appear_in_cli_errors(self):
        marker = "synthetic-private-input"
        result = subprocess.run([sys.executable, str(source), "--source-uid", marker], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(result.stderr.strip(), "Read-only retained restore inspection failed; private diagnostics suppressed.")

    def test_journal_mode_requires_internal_ext4_journal(self):
        value = bytearray(1024)
        value[56:58] = b"\x53\xef"
        struct.pack_into("<I", value, 92, 4)
        struct.pack_into("<I", value, 224, 8)
        disk.internal_journal(value)
        for offset in [56, 92, 208, 224, 228]:
            bad = bytearray(value)
            bad[offset] = 1 if offset != 224 else 0
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                disk.internal_journal(bad)
        with self.assertRaises(ValueError):
            disk.internal_journal(value[:100])

    def test_journal_arguments_never_enable_general_repair(self):
        self.assertEqual(disk.journal_arguments("/pinned/e2fsck", Path("/private/additional.raw")),
                         ["/pinned/e2fsck", "-p", "-E", "journal_only", "/private/additional.raw"])

    def test_only_clean_or_corrected_exit_is_accepted(self):
        for status in [0, 1]:
            disk.journal_status(status)
        for status in [True, -15, 2, 3, 4, 8, 16, 32, 128, 129]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                disk.journal_status(status)


if __name__ == "__main__":
    unittest.main()
