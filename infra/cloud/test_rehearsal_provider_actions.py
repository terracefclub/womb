import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import rehearsal_lease as lease
import rehearsal_provider_actions as actions
from test_rehearsal_lease import RUN, PREFIX, FakeApi, inventory, populate, resource


class ProviderActionsTest(unittest.TestCase):
    def test_console_only_accepts_official_host_and_exact_server(self):
        url = "wss://console.hetzner.cloud/?server_id=4&token=synthetic-opaque-token"
        self.assertEqual(actions.console_url(url, 4), url)
        for bad in (url.replace("server_id=4", "server_id=1"), url.replace("wss:", "ws:"),
                    url.replace("hetzner.cloud", "hetzner.cloud.example"), url + "&server_id=4", url + "#extra"):
            with self.assertRaises(lease.Rejected):
                actions.console_url(bad, 4)

    def test_rescue_console_and_installed_boot_use_only_owned_server_and_key(self):
        data = inventory()
        manifest = {"run": RUN, "baseline": lease.baseline_from(data), "expires": int(time.time()) + 1200}
        populate(data, "original", 4, 5)
        row = data["servers"][1]
        row.update(rescue_enabled=False, status="running")
        row["public_net"]["ipv4"]["ip"] = "93.184.215.14"
        data["firewalls"] = [resource(8, PREFIX + "-firewall", applied_to=[])]
        data["ssh_keys"].append(resource(9, PREFIX + "-key", public_key="ssh-ed25519 unexpected"))
        api = FakeApi(data)
        calls = []

        def request(route, *, post):
            self.assertTrue(route.startswith("servers/4/actions/"))
            operation = route.rsplit("/", 1)[-1]
            calls.append(operation)
            result = {"action": {"id": len(calls)}}
            if operation == "enable_rescue":
                self.assertEqual(post, {"type": "linux64", "ssh_keys": [9]})
                row["rescue_enabled"] = True
                result["root_password"] = "synthetic-root-password"
            if operation == "request_console":
                result.update(wss_url="wss://console.hetzner.cloud/?server_id=4&token=synthetic-opaque-token",
                              password="synthetic-vnc-password")
            return result

        api.request = request
        api.action_status = lambda _rid: "success"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            lease.write_private(root / "foundation-created.json", {"ssh_keys": [1]})
            lease.write_private(root / "foundation-input.json", {"sshPublicKey": "ssh-ed25519 synthetic test"})
            lease.write_private(root / "original-created.json", {"servers": [4], "primary_ips": [5]})
            with (patch.object(actions.lease, "Api", return_value=api), patch.object(actions.apply, "require_run_unit"),
                  patch.object(actions.timer, "check_ready")):
                args = (root, manifest, root / "credential", b"synthetic", "original", "app")
                with self.assertRaises(lease.Rejected):
                    actions.operate("rescue", *args)
                self.assertEqual(calls, [])
                (root / "foundation-created.json").unlink()
                lease.write_private(root / "foundation-created.json", {"ssh_keys": [9]})
                with self.assertRaises(lease.Rejected):
                    actions.operate("rescue", *args)
                self.assertEqual(calls, [])
                data["ssh_keys"][1]["public_key"] = "ssh-ed25519 synthetic"
                actions.operate("rescue", *args)
                actions.operate("console", *args)
                receipt, = root.glob("original-app-console-*.json")
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                console = json.loads(lease.read_private(receipt, 4096))
                self.assertEqual(console["serverId"], 4)
                self.assertLessEqual(console["expires"] - console["observedAt"], 55)
                binding = json.loads(lease.read_private(root / "original-app-rescue-boot.json", 4096))
                lease.write_private(root / "original-app-installed.json", {"binding": binding, "mountedExt4Verified": True})
                actions.operate("boot-installed", *args)
                with self.assertRaises(lease.Rejected):
                    actions.operate("console", *args)
            self.assertEqual(calls, ["enable_rescue", "reboot", "request_console", "disable_rescue", "reboot"])


if __name__ == "__main__":
    unittest.main()
