import base64
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import console_fingerprint as console


class ConsoleFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.binding = {"run": "a" * 32, "phase": "original", "role": "app",
                        "serverId": 55, "ipv4": "1.1.1.1", "expires": 1300}
        self.receipt = {**self.binding, "observedAt": 1000, "expires": 1060,
                        "wssUrl": "wss://console.hetzner.cloud/?server_id=55&token=synthetic-placeholder",
                        "consoleAction": {"id": 82, "serverId": 55},
                        "vncPassword": "synthetic-password", "rescuePassword": "synthetic-password"}

    def test_console_only_exact_provider_role_and_expiry(self):
        self.assertEqual(console.validate_receipt(self.receipt, self.binding, 1010), 82)
        for delta, now in [
            ({"role": "relay"}, 1010),
            ({"serverId": 56}, 1010),
            ({"serverId": "55"}, 1010),
            ({"consoleAction": {"id": True, "serverId": 55}}, 1010),
            ({"consoleAction": {"id": 82, "serverId": 56}}, 1010),
            ({"expires": 1061}, 1010),
            ({}, 1060),
            ({"wssUrl": self.receipt["wssUrl"].replace("55", "56")}, 1010),
            ({"wssUrl": self.receipt["wssUrl"].replace("hetzner.cloud", "hetzner.cloud.example")}, 1010),
            ({"wssUrl": self.receipt["wssUrl"].replace("wss://", "wss://unexpected@")}, 1010),
            ({"wssUrl": self.receipt["wssUrl"].replace("placeholder", "%0Aplaceholder")}, 1010),
            ({"wssUrl": self.receipt["wssUrl"] + "&token=duplicate"}, 1010),
        ]:
            with self.subTest(now=now, fields=list(delta)):
                with self.assertRaises(ValueError):
                    console.validate_receipt({**self.receipt, **delta}, self.binding, now)

    def test_opaque_endpoint_requires_matching_action_journal_before_ssh(self):
        query = urlencode({key: base64.b64encode(b"\xff" * size).decode("ascii")
                           for key, size in (("data", 116), ("nonce", 12), ("tag", 16))})
        receipt = {**self.receipt, "wssUrl": "wss://web-console.hetzner.cloud/pp?" + query}
        self.assertEqual(console.validate_receipt(receipt, self.binding, 1010), 82)
        directory = Path("/synthetic-lease")
        path = directory / "original-app-console-1.json"
        with (patch.object(console.rescue, "lease_binding", return_value=self.binding),
              patch.object(console.rescue, "read_json", side_effect=[(receipt, ""), ({"id": 83}, "")]) as read,
              patch.object(console.time, "time", return_value=1010),
              patch.object(console.rescue, "checked_process") as ssh):
            with self.assertRaisesRegex(ValueError, "action journal disagrees"):
                console.enroll(directory, "original", "app", path, {})
        self.assertEqual(read.call_args.args[0], directory / "original-action-82.json")
        ssh.assert_not_called()

    def test_quiet_keyscan_preserves_strict_candidate_shape(self):
        directory = Path("/synthetic-lease")
        path = directory / "original-app-console-1.json"
        key = b"1.1.1.1 ssh-ed25519 " + base64.b64encode(console.rescue.KEY_PREFIX + b"x" * 32)
        noisy = b"# 1.1.1.1:22 SSH-2.0-synthetic\n" + key
        with (patch.object(console.rescue, "lease_binding", return_value=self.binding),
              patch.object(console.rescue, "read_json", side_effect=[(self.receipt, ""), ({"id": 82}, "")]),
              patch.object(console.time, "time", return_value=1010),
              patch.object(console.rescue, "checked_process", return_value=noisy) as ssh,
              patch.object(console, "attest_console") as attest):
            with self.assertRaises(ValueError):
                console.enroll(directory, "original", "app", path, {})
        ssh.assert_called_once_with(["ssh-keyscan", "-q", "-T", "5", "-t", "ed25519", "1.1.1.1"],
                                    8, capture=True)
        attest.assert_not_called()

    def test_connection_diagnostics_keep_only_exact_socket_and_fixed_fields(self):
        cdp = Mock()
        facts = {}
        url = self.receipt["wssUrl"]
        console.observe_socket(cdp, url, facts)
        callbacks = dict(call.args for call in cdp.on.call_args_list)
        cdp.send.assert_called_once_with("Network.enable")
        self.assertEqual(set(callbacks), {"Network.webSocketCreated", "Network.webSocketHandshakeResponseReceived",
                                          "Network.webSocketFrameError"})
        created = callbacks["Network.webSocketCreated"]
        response = callbacks["Network.webSocketHandshakeResponseReceived"]
        failed = callbacks["Network.webSocketFrameError"]
        created({"requestId": "other", "url": url + "&unexpected=1"})
        response({"requestId": "other", "response": {"status": 401}})
        failed({"requestId": "other", "errorMessage": "synthetic credential"})
        self.assertEqual(facts, {"cdpAttached": True})
        created({"requestId": "expected", "url": url})
        response({"requestId": "expected", "response": {"status": True}})
        self.assertNotIn("handshakeStatus", facts)
        response({"requestId": "expected", "response": {"status": 101, "headers": {"x-secret": "synthetic"}}})
        failed({"requestId": "expected", "errorMessage": "synthetic credential"})
        console.record_rfb_facts(facts, {"securityFailure": True, "credentialsRequired": "synthetic credential",
                                         "initState": "Security", "other": "synthetic credential"})
        console.record_rfb_facts(facts, {"securityFailure": True, "initState": "synthetic credential"})
        self.assertEqual(facts, {"cdpAttached": True, "socketCreated": True, "handshakeStatus": 101,
                                 "frameError": True, "securityFailure": True, "credentialsRequired": False,
                                 "disconnected": False, "rfbInitState": "Security"})

    def test_marker_cannot_be_observed_from_echoed_command(self):
        key = console.rescue.KEY_PREFIX + b"x" * 32
        raw = b"1.1.1.1 ssh-ed25519 " + base64.b64encode(key)
        fingerprint = console.candidate_key(raw, "1.1.1.1")
        marker = "TERRACEABCDEFGHIJKL"
        command = console.console_command(fingerprint, marker)
        self.assertNotIn(marker, command)
        self.assertFalse(console.marker_seen("root@rescue # " + command, marker))
        self.assertTrue(console.marker_seen("root@rescue #\n" + marker + "\nroot@rescue #", marker))
        self.assertFalse(console.marker_seen(marker + "\n" + marker, marker))
        self.assertFalse(console.marker_seen(marker + "suffix", marker))

    def test_password_send_requires_console_prompt_shapes(self):
        self.assertTrue(console.LOGIN.search("rescue login: "))
        self.assertFalse(console.LOGIN.search("note: login: failed"))
        self.assertTrue(console.PASSWORD.search("Password:"))
        self.assertFalse(console.PASSWORD.search("password: some value"))
        self.assertTrue(console.ROOT.search("root@rescue ~ #"))
        self.assertFalse(console.ROOT.search("someone #"))

    def test_root_wait_diagnostics_accumulate_only_fixed_observations(self):
        progress = {}
        accepts = console.root_prompt_observer(progress)
        initial = {"loginIncorrect": False, "loginPromptVisible": False,
                   "passwordPromptReappeared": False, "rootLikeWithoutExactPrompt": False}
        self.assertFalse(accepts("Password:\nprivate fixture text"))
        self.assertEqual(progress, {"rootWait": initial})
        self.assertFalse(accepts("note: Login incorrect (synthetic)\nsecret synthetic text"))
        self.assertEqual(progress, {"rootWait": initial})
        self.assertFalse(accepts(" Login incorrect \nrescue login:\nPassword:"))
        self.assertFalse(accepts(" root @ rescue ~ #"))
        self.assertTrue(accepts("root@rescue ~ #"))
        self.assertEqual(progress, {"rootWait": dict.fromkeys(initial, True)})

    def test_keyboard_calibration_requires_active_echo_and_empty_line_after_erasing(self):
        page = Mock()
        progress = {}
        prompt = "rescue login:"
        frames = iter([
            prompt + "\nnot the active line", prompt,
            f"{prompt} {console.KEYBOARD_SAMPLE}\nnot the active line",
            f"{prompt} {console.KEYBOARD_SAMPLE}",
            prompt + "\nnot erased yet", prompt,
            f"{prompt} {console.KEYBOARD_SWAPPED}", prompt,
        ])
        with patch.object(console.time, "sleep"):
            console.calibrate_keyboard(page, lambda: next(frames), progress, console.time.monotonic() + 60)
        self.assertEqual(progress, {"keyboardCalibration": {
            "immediate": {"echo": "asExpected", "erased": True},
            "paced": {"echo": "yZSwap", "erased": True},
        }})
        self.assertEqual([call.args for call in page.evaluate.call_args_list], [
            ("value => window.consoleCalibration(value)", {"sample": console.KEYBOARD_SAMPLE, "paced": paced, "erase": erase})
            for paced, erase in ((False, False), (True, True), (True, False), (True, True))
        ])

    def test_keyboard_calibration_stops_if_active_prompt_is_not_restored(self):
        page = Mock()
        prompt = "rescue login:"
        frames = iter([prompt, f"{prompt} {console.KEYBOARD_SAMPLE}"])
        progress = {}
        ticks = iter(range(25))
        with (patch.object(console.time, "monotonic", side_effect=lambda: next(ticks)),
              patch.object(console.time, "sleep")):
            with self.assertRaisesRegex(ValueError, "not restored"):
                console.calibrate_keyboard(page, lambda: next(frames, "private unknown text"), progress, 60)
        self.assertEqual(page.evaluate.call_count, 2)
        self.assertEqual(progress, {"keyboardCalibration": {
            "immediate": {"echo": "asExpected", "erased": False},
            "paced": {"echo": "unknown", "erased": False},
        }})

    def test_keyboard_probe_requires_review_and_never_discovers_or_pins_ssh_key(self):
        directory = Path("/synthetic-lease")
        path = directory / "original-relay-console-1.json"
        binding = {**self.binding, "role": "relay"}
        receipt = {**self.receipt, "role": "relay"}
        result = {"immediate": {"echo": "unknown", "erased": True}, "paced": {"echo": "asExpected", "erased": True}}
        progress = {}
        with (patch.object(console.rescue, "lease_binding", return_value=binding),
              patch.object(console.rescue, "read_json", side_effect=[(receipt, ""), ({"id": 82}, "")] * 2),
              patch.object(console.rescue, "reviewed_probe_mode", side_effect=[False, True]) as reviewed,
              patch.object(console.time, "time", return_value=1010),
              patch.object(console.time, "time_ns", return_value=123),
              patch.object(console.rescue, "checked_process") as ssh,
              patch.object(console, "record_text") as pin,
              patch.object(console.rescue, "write_private") as write,
              patch.object(console, "attest_console", side_effect=lambda *_args, **_opts:
                           progress.update(keyboardCalibration=result)) as browser):
            with self.assertRaisesRegex(ValueError, "requires a reviewed original relay probe"):
                console.enroll(directory, "original", "relay", path, progress, keyboard_probe=True)
            browser.assert_not_called()
            console.enroll(directory, "original", "relay", path, progress, keyboard_probe=True)
        self.assertEqual(reviewed.call_count, 2)
        browser.assert_called_once_with(receipt, None, progress, keyboard_probe=True)
        write.assert_called_once_with(directory / "original-relay-keyboard-calibration-123.json", result)
        ssh.assert_not_called()
        pin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
