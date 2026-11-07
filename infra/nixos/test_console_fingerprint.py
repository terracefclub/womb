import base64
import unittest

import console_fingerprint as console


class ConsoleFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.binding = {"run": "a" * 32, "phase": "original", "role": "app",
                        "serverId": 55, "ipv4": "1.1.1.1", "expires": 1300}
        self.receipt = {**self.binding, "observedAt": 1000, "expires": 1060,
                        "wssUrl": "wss://console.hetzner.cloud/?server_id=55&token=synthetic-placeholder",
                        "vncPassword": "synthetic-password", "rescuePassword": "synthetic-password"}

    def test_console_only_exact_provider_role_and_expiry(self):
        console.validate_receipt(self.receipt, self.binding, 1010)
        for delta, now in [
            ({"role": "relay"}, 1010),
            ({"serverId": 56}, 1010),
            ({"serverId": "55"}, 1010),
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


if __name__ == "__main__":
    unittest.main()
