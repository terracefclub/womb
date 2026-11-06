"""Pure protocol-fixture checks; actual app/browser acceptance is the VM test."""
import contextlib
import io
import json
import importlib.util
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

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

    def test_login_facts_reset_attempt_and_bound_event_counters(self):
        first = fixture.fresh_login_diagnostics(1)
        for _ in range(300):
            fixture.login_counter(first, "pageErrors")
        first["callbackStatus"] = 403
        first["sessionEmpty"] = True
        second = fixture.fresh_login_diagnostics(2)
        self.assertEqual(first["pageErrors"], 255)
        self.assertEqual(second["pageErrors"], 0)
        self.assertEqual(second["callbackStatus"], 0)
        self.assertIsNone(second["sessionEmpty"])
        self.assertEqual(second["ordinal"], 2)
        with self.assertRaises(AssertionError):
            fixture.login_counter(second, "PRIVATE_URL")
        for invalid in (0, 101, True, "1"):
            with self.assertRaises(AssertionError):
                fixture.fresh_login_diagnostics(invalid)

    def test_login_exception_labels_never_reflect_messages_or_class_names(self):
        marker = "PRIVATE_TOKEN_URL_PASSWORD"
        timeout = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
        unknown = type(marker, (Exception,), {})
        self.assertEqual(fixture.login_exception_class(timeout(marker)), "PlaywrightTimeout")
        self.assertEqual(fixture.login_exception_class(AssertionError(marker)), "AssertionError")
        self.assertEqual(fixture.login_exception_class(unknown(marker)), "other")

    def test_first_provider_click_has_early_listeners_and_exact_failure_step(self):
        page = MagicMock()
        page.goto.return_value.status = 200
        page.is_closed.return_value = True
        browser = MagicMock()
        browser.new_context.return_value.new_page.return_value = page
        engine = MagicMock()
        engine.__enter__.return_value.chromium.launch.return_value = browser
        api = SimpleNamespace(sync_playwright=lambda: engine)
        def fail_click():
            handlers = dict((call.args[0], call.args[1]) for call in page.on.call_args_list)
            self.assertEqual(set(handlers), {"pageerror", "requestfailed", "response"})
            handlers["pageerror"](Exception("PRIVATE_CALLBACK_TOKEN"))
            handlers["requestfailed"](SimpleNamespace(url="https://PRIVATE_CALLBACK_TOKEN"))
            handlers["response"](SimpleNamespace(url="https://documents.terrace.invalid/api/auth/opaque?PRIVATE_CALLBACK_TOKEN", status=503, request=SimpleNamespace(resource_type="fetch")))
            raise AssertionError("PRIVATE_CALLBACK_TOKEN")
        page.get_by_role.return_value.click.side_effect = fail_click
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.dict(sys.modules, {"playwright.sync_api": api}), patch.dict(fixture.os.environ, {"TERRACE_BROWSER_BINARY": "unused"}), patch.object(fixture, "ROOT", Path(directory)), patch.object(fixture, "private_read", return_value={"users": []}), patch.object(fixture, "login_diagnostics", None), patch.object(sys, "argv", ["browser", "create"]), contextlib.redirect_stderr(output):
                with self.assertRaises(SystemExit):
                    fixture.entrypoint()
            line = next(line for line in output.getvalue().splitlines() if line.startswith("Documenso bounded login facts: "))
            facts = json.loads(line.removeprefix("Documenso bounded login facts: "))
            self.assertEqual(facts["step"], "provider-click")
            self.assertEqual(facts["ordinal"], 1)
            self.assertEqual(facts["signinStatus"], 200)
            self.assertEqual(facts["callbackStatus"], 0)
            self.assertEqual([facts[key] for key in ["pageErrors", "failedRequests", "authResponses", "authHttpErrors"]], [1, 1, 1, 1])
            self.assertNotIn("PRIVATE_CALLBACK_TOKEN", output.getvalue())
            browser.new_context.return_value.set_default_timeout.assert_called_once_with(45000)
            browser.close.assert_called_once()

    def test_entrypoint_emits_only_fixed_login_facts_and_current_exception(self):
        marker = "PRIVATE_TOKEN_URL_PASSWORD"
        for step, expected in [("provider-click", "AssertionError"), ("passed", "not-login")]:
            facts = fixture.fresh_login_diagnostics(1)
            facts["step"] = step
            output = io.StringIO()
            with patch.object(fixture, "login_diagnostics", facts), patch.object(fixture, "stage", "closed signup and unapproved account denial"), patch.object(fixture, "main", side_effect=AssertionError(marker)), contextlib.redirect_stderr(output):
                with self.assertRaises(SystemExit) as exited:
                    fixture.entrypoint()
            self.assertEqual(exited.exception.code, 1)
            line = output.getvalue().splitlines()[0]
            observed = json.loads(line.removeprefix("Documenso bounded login facts: "))
            self.assertEqual(observed["exceptionClass"], expected)
            self.assertEqual(observed["step"], step)
            self.assertNotIn(marker, output.getvalue())
            self.assertTrue(all(type(value) in (str, int, bool, type(None)) for value in observed.values()))

    def test_backup_code_digest_is_stable_without_retaining_codes(self):
        self.assertEqual(fixture.digest({"b": 2, "a": 1}), fixture.digest({"a": 1, "b": 2}))
        self.assertNotEqual(fixture.digest(["synthetic-code"]), fixture.digest(["changed-code"]))
        self.assertRegex(fixture.digest(["synthetic-code"]), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
