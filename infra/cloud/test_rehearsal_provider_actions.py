import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import rehearsal_lease as lease
import rehearsal_provider_actions as actions
from test_rehearsal_lease import RUN, PREFIX, FakeApi, inventory, populate, resource, write_reviewed_input


class ProviderActionsTest(unittest.TestCase):
    def test_console_only_accepts_official_host_and_exact_server(self):
        url = "wss://console.hetzner.cloud/?server_id=4&token=synthetic-opaque-token"
        self.assertEqual(lease.console_url(url, 4), url)
        for bad in (url.replace("server_id=4", "server_id=1"), url.replace("wss:", "ws:"),
                    url.replace("hetzner.cloud", "hetzner.cloud.example"),
                    url.replace("console.hetzner.cloud", "web-console.hetzner.cloud"), url + "&server_id=4", url + "#extra"):
            with self.assertRaises(lease.Rejected):
                lease.console_url(bad, 4)

    def test_opaque_console_grant_has_fixed_host_path_and_encoding(self):
        grant = {key: base64.b64encode(b"\xff" * size).decode()
                 for key, size in (("data", 117), ("nonce", 12), ("tag", 16))}
        route = "wss://web-console.hetzner.cloud/pp?"
        url = route + urlencode(grant)
        self.assertEqual(lease.console_url(url, 4), url)
        for bad in (url.replace("/pp?", "/other?"), url.replace("web-console", "console"),
                    route + urlencode({**grant, "tag": "AAAA"}), url + "&nonce=duplicate",
                    route + urlencode({**grant, "data": "***"}), url + "#extra"):
            with self.assertRaises(lease.Rejected):
                lease.console_url(bad, 4)

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
        console_server = 1

        def request(route, *, post=None):
            if post is None:
                self.assertEqual(route, f"actions/{len(calls)}")
                return {"action": {"id": len(calls), "status": "success", "command": "request_console",
                                   "resources": [{"type": "server", "id": console_server}]}}
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
            write_reviewed_input(root, {"phase": "original", "run": RUN})
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
                with self.assertRaises(lease.Rejected):
                    actions.operate("console", *args)
                self.assertEqual(list(root.glob("original-app-console-*.json")), [])
                console_server = 4
                actions.operate("console", *args)
                receipt, = root.glob("original-app-console-*.json")
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                console = json.loads(lease.read_private(receipt, 4096))
                self.assertEqual(console["serverId"], 4)
                self.assertEqual(console["consoleAction"], {"id": 4, "serverId": 4})
                self.assertLessEqual(console["expires"] - console["observedAt"], 55)
                binding = json.loads(lease.read_private(root / "original-app-rescue-boot.json", 4096))
                lease.write_private(root / "original-app-installed.json", {"binding": binding, "mountedExt4Verified": True})
                actions.operate("boot-installed", *args)
                with self.assertRaises(lease.Rejected):
                    actions.operate("console", *args)
            self.assertEqual(calls, ["enable_rescue", "reboot", "request_console", "request_console",
                                     "disable_rescue", "reboot"])

    def test_exact_console_probe_uses_password_only_rescue_and_blocks_installed_boot(self):
        data = inventory()
        manifest = {"run": RUN, "baseline": lease.baseline_from(data), "expires": int(time.time()) + 1200}
        populate(data, "original", 4, 5)
        name = PREFIX + "-original-relay"
        data["servers"][1].update(name=name, rescue_enabled=False, status="running")
        data["servers"][1]["public_net"]["ipv4"]["ip"] = "93.184.215.14"
        data["primary_ips"][1]["name"] = name + "-ip"
        data["firewalls"] = [resource(8, PREFIX + "-firewall")]
        data["ssh_keys"].append(resource(9, PREFIX + "-key", public_key="ssh-ed25519 synthetic"))
        api = FakeApi(data)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"phase": "original", "run": RUN, "probeOnly": True}
            config_path = write_reviewed_input(root, config)
            lease.write_private(root / "original-created.json", {"servers": [4], "primary_ips": [5]})
            lease.write_private(root / "foundation-created.json", {"ssh_keys": [9]})
            lease.write_private(root / "foundation-input.json", {"sshPublicKey": "ssh-ed25519 synthetic"})
            with (patch.object(actions.lease, "Api", return_value=api), patch.object(actions.apply, "require_run_unit"),
                  patch.object(actions.timer, "check_ready"),
                  patch.object(actions, "dispatch", return_value={"root_password": "synthetic-password"}) as dispatch):
                args = (root, manifest, root / "credential", b"synthetic", "original", "relay")
                config_path.unlink()
                lease.write_private(config_path, {**config, "probeOnly": False})
                with self.assertRaisesRegex(lease.Rejected, "no longer matches"):
                    actions.operate("rescue", *args)
                dispatch.assert_not_called()
                write_reviewed_input(root, config)
                with self.assertRaisesRegex(lease.Rejected, "probe action scope"):
                    actions.operate("boot-installed", *args)
                dispatch.assert_not_called()
                actions.operate("rescue", *args)
                self.assertEqual([call.args[-2:] for call in dispatch.call_args_list],
                                 [("enable_rescue", {"type": "linux64", "ssh_keys": []}), ("reboot", {})])


if __name__ == "__main__":
    unittest.main()
