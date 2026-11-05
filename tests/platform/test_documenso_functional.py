"""Pure protocol-fixture checks; actual app/browser acceptance is the VM test."""
import importlib.util
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("documenso_browser", Path(__file__).resolve().parents[2] / "platform/documenso/functional-browser.py")
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class FunctionalFixtureTests(unittest.TestCase):
    def test_existing_unsigned_modes_require_explicit_signing_opt_in(self):
        for mode in ["create", "verify"]:
            self.assertEqual(fixture.mode_options([mode]), (mode, False))
            self.assertEqual(fixture.mode_options([mode, "--signing"]), (mode, True))
        for args in [[], ["other"], ["disabled", "--signing"], ["create", "--other"], ["verify", "--signing", "extra"]]:
            with self.assertRaises(ValueError):
                fixture.mode_options(args)

    def test_totp_rfc6238_six_digit_vector(self):
        with patch.object(fixture.time, "time", return_value=59):
            self.assertEqual(fixture.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), "287082")

    def test_pdf_offsets_and_stream_are_consistent(self):
        value = fixture.pdf_fixture()
        self.assertTrue(value.startswith(b"%PDF-1.4"))
        self.assertIn(b"Synthetic rehearsal only - unsigned", value)
        xref = int(value.split(b"startxref\n")[1].splitlines()[0])
        self.assertEqual(value[xref:xref + 4], b"xref")
        offsets = value[xref:].splitlines()[3:8]
        for index, offset in enumerate(offsets, 1):
            start = int(offset.split()[0])
            self.assertTrue(value[start:].startswith(f"{index} 0 obj\n".encode()))
        self.assertLess(len(value), 4096)

    def test_protected_receipt_is_exclusive_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            fixture.private_write(path, {"safe": "fixture"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(fixture.private_read(path), {"safe": "fixture"})
            with self.assertRaises(FileExistsError):
                fixture.private_write(path, {})
            self.assertEqual(fixture.private_read(path), {"safe": "fixture"})

    def test_rejects_public_oversized_and_symlink_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(AssertionError):
                fixture.private_read(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(AssertionError):
                fixture.private_read(link)
            path.write_text(" " * 131073)
            with self.assertRaises(AssertionError):
                fixture.private_read(path)

    def test_corrupt_private_json_never_reaches_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"PRIVATE_MARKER_MUST_NOT_APPEAR": truncated')
            path.chmod(0o600)
            code = "import importlib.util,sys;from pathlib import Path;" + \
                "s=importlib.util.spec_from_file_location('probe',sys.argv[1]);" + \
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);" + \
                "m.ROOT=Path(sys.argv[2]);sys.argv=['probe','create'];m.entrypoint()"
            result = subprocess.run([sys.executable, "-c", code, str(SPEC.origin), directory],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "Documenso functional check failed at private fixture validation; private diagnostics suppressed.\n")
            self.assertNotIn("PRIVATE_MARKER", result.stderr)

    def test_backup_code_digest_is_stable_without_retaining_codes(self):
        self.assertEqual(fixture.digest({"b": 2, "a": 1}), fixture.digest({"a": 1, "b": 2}))
        self.assertNotEqual(fixture.digest(["synthetic-code"]), fixture.digest(["changed-code"]))
        self.assertRegex(fixture.digest(["synthetic-code"]), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
