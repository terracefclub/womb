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

    def test_request_facts_are_bounded_labels_without_values(self):
        fixture.request_diagnostics.clear()
        response = SimpleNamespace(status=200, headers={"date": "Thu, 01 Jan 1970 00:01:30 GMT", "set-cookie": "secret=value"})
        with patch.object(fixture.time, "time", return_value=89.5), patch.object(fixture.time, "monotonic", return_value=12.25):
            fixture.request_fact("download original", 12.0, response)
            timeout = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
            fixture.request_fact("two-factor view-recovery-codes", 12.0, error=timeout())
        self.assertEqual(fixture.request_diagnostics[0], {"request": "download original", "ms": 250, "status": 200, "totpStepSecond": 29, "serverClockSkewSeconds": 0, "serverStepSecond": 0})
        self.assertEqual(fixture.request_diagnostics[1]["exceptionClass"], "PlaywrightTimeout")
        self.assertNotIn("secret", json.dumps(fixture.request_diagnostics))
        for index in range(20):
            fixture.request_fact("download signed", 12.0, response)
        self.assertEqual(len(fixture.request_diagnostics), 12)

    def test_totp_rfc6238_six_digit_vector(self):
        with patch.object(fixture.time, "time", return_value=45):
            self.assertEqual(fixture.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), "287082")  # RFC 6238 SHA-1 vector, step 1

    def boundary(self, start):
        # A fake clock whose sleep advances it: drives the step boundary without waiting.
        clock = SimpleNamespace(now=start, slept=[])
        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += seconds
        with patch.object(fixture.time, "time", side_effect=lambda: clock.now), patch.object(fixture.time, "sleep", side_effect=sleep):
            code = fixture.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        return code, clock

    def test_totp_is_never_generated_at_the_end_of_a_step(self):
        # Documenso v2.21.1 checks only the server's current step and answers a rejected code with 500: a code made
        # at second 29.5 reaches a server one second ahead in the next step (the 2026-11-23 rehearsal failure).
        code, clock = self.boundary(59.5)
        self.assertEqual(code, "359152")  # step 2 (RFC 6238 counter 2), not step 1's "287082"
        self.assertEqual(clock.slept, [3.5])
        self.assertGreaterEqual(clock.now % 30, 3)

    def test_totp_is_never_generated_at_the_start_of_a_step(self):
        # A server behind the client would still be in the previous step.
        code, clock = self.boundary(60.5)
        self.assertEqual(code, "359152")
        self.assertEqual(clock.slept, [2.5])

    def test_totp_mid_step_does_not_wait(self):
        code, clock = self.boundary(45)
        self.assertEqual((code, clock.slept), ("287082", []))

    @staticmethod
    def hotp(counter):
        # Independent RFC 4226 reference for the RFC 6238 test secret ("12345678901234567890").
        import hashlib, hmac, struct
        value = hmac.new(b"12345678901234567890", struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = value[-1] & 15
        return str((struct.unpack(">I", value[offset:offset + 4])[0] & 0x7fffffff) % 1000000).zfill(6)

    def stepping(self, start, jumps, guard=None):
        # A fake clock whose sleep advances it by the pause plus a clock step (jumps[i] for the i-th sleep).
        clock = SimpleNamespace(now=start, slept=[])
        def sleep(seconds):
            clock.now += seconds + (jumps[len(clock.slept)] if len(clock.slept) < len(jumps) else 0)
            clock.slept.append(seconds)
        with patch.object(fixture.time, "time", side_effect=lambda: clock.now), patch.object(fixture.time, "sleep", side_effect=sleep):
            # RFC 6238's published SHA-1 test seed, base32.
            arguments = ["GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"] + ([] if guard is None else [guard])
            code = fixture.totp(*arguments)
        return code, clock

    def test_totp_checks_the_clock_again_after_its_wait(self):
        # The clock steps forward 26.5 s during the first wait and lands at second 29.5 of the next step:
        # the code must not be made there; a second wait reaches second 3 of the step after.
        code, clock = self.stepping(59.5, [26.5])
        self.assertEqual(clock.slept, [3.5, 3.5])
        self.assertEqual(clock.now, 93.0)
        self.assertEqual(code, self.hotp(3))

    def test_totp_gives_up_after_bounded_waits_with_a_clear_message(self):
        # A clock that keeps stepping back to the end of a step: three waits, then a fixed message.
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(AssertionError):
            self.stepping(59.5, [-3.5, -3.5, -3.5, -3.5])
        self.assertEqual(output.getvalue(), "Documenso TOTP guard: the clock was still within 3 s of a step edge after 3 waits.\n")

    def test_totp_records_whether_the_guard_fired_and_the_request_deadline(self):
        guard = {}
        code, _clock = self.stepping(59.5, [], guard)
        self.assertEqual(code, self.hotp(2))
        self.assertEqual(guard, {"codeStep": 2, "codeStepSecond": 3.0, "guardWaits": 1, "guardSleptMs": 3500, "deadlineMs": 26000})
        guard = {}
        self.stepping(45.25, [], guard)
        self.assertEqual(guard, {"codeStep": 1, "codeStepSecond": 15.2, "guardWaits": 0, "guardSleptMs": 0, "deadlineMs": 13750})

    def test_request_fact_records_the_code_and_end_step(self):
        fixture.request_diagnostics.clear()
        guard = {"codeStep": 2, "codeStepSecond": 27.0, "guardWaits": 0, "guardSleptMs": 0, "deadlineMs": 2000}
        response = SimpleNamespace(status=500, headers={})
        with patch.object(fixture.time, "time", return_value=90.4), patch.object(fixture.time, "monotonic", return_value=15.0):
            fixture.request_fact("two-factor view-recovery-codes", 12.0, response, guard=guard)
        fact = fixture.request_diagnostics[-1]
        self.assertEqual((fact["endStepSecond"], fact["sameClientStep"], fact["guardWaits"], fact["codeStepSecond"]), (0.4, False, 0, 27.0))

    def two_factor(self, behaviour, elapsed, guard):
        # post() against a fake request context; monotonic time advances by `elapsed` during the request.
        clock = SimpleNamespace(now=100.0)
        calls = []
        def request(url, **options):
            calls.append(options)
            clock.now += elapsed
            return behaviour()
        ctx = SimpleNamespace(request=SimpleNamespace(post=request))
        output = io.StringIO()
        fixture.request_diagnostics.clear()
        with patch.object(fixture.time, "monotonic", side_effect=lambda: clock.now), patch.object(fixture.time, "time", return_value=63.0), \
                contextlib.redirect_stderr(output):
            try:
                result = fixture.post(ctx, "view-recovery-codes", {"token": "000000"}, guard)
            except BaseException as error:
                result = error
        return result, calls, output.getvalue()

    def test_two_factor_request_gets_the_time_left_in_the_step_as_its_timeout(self):
        guard = {"codeStep": 2, "codeStepSecond": 3.0, "guardWaits": 1, "guardSleptMs": 3500, "deadlineMs": 26000}
        ok = SimpleNamespace(status=200, headers={}, body=lambda: b"{}", json=lambda: {"success": True, "backupCodes": []})
        result, calls, output = self.two_factor(lambda: ok, 0.25, guard)
        self.assertEqual(result, {"success": True, "backupCodes": []})
        self.assertEqual(calls[0]["timeout"], 26000)
        line = json.loads(output.removeprefix("Documenso TOTP request: "))
        self.assertEqual((line["guardWaits"], line["guardSleptMs"], line["codeStepSecond"], line["ms"], line["status"], line["sameClientStep"]), (1, 3500, 3.0, 250, 200, True))
        self.assertNotIn("000000", output)

    def test_an_answer_after_the_margin_fails_naming_the_cause_not_the_500(self):
        guard = {"codeStep": 2, "codeStepSecond": 27.0, "guardWaits": 0, "guardSleptMs": 0, "deadlineMs": 2000}
        late = SimpleNamespace(status=500, headers={}, body=lambda: b"{}", json=lambda: {"success": False})
        result, _calls, output = self.two_factor(lambda: late, 2.5, guard)
        self.assertIsInstance(result, AssertionError)
        self.assertEqual(str(result), "TOTP margin outlasted")
        self.assertIn("Documenso two-factor view-recovery-codes: request outlasted the TOTP margin (2500 ms, deadline 2000 ms after a code made at step second 27.0)", output)
        self.assertIn("any late answer, even a success, is treated as a failure.", output)

    def test_a_request_timeout_at_the_margin_names_the_cause(self):
        guard = {"codeStep": 2, "codeStepSecond": 27.0, "guardWaits": 0, "guardSleptMs": 0, "deadlineMs": 2000}
        timeout = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
        def raises():
            raise timeout("PRIVATE_URL")
        result, _calls, output = self.two_factor(raises, 2.0, guard)
        self.assertIsInstance(result, timeout)
        self.assertIn("request outlasted the TOTP margin (2000 ms, deadline 2000 ms", output)
        self.assertIn('"exceptionClass": "PlaywrightTimeout"', output)
        self.assertNotIn("PRIVATE_URL", output)

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
