import base64
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import rescue_install as rescue
import stock_kexec
from test_stock_kexec_native import (
    FIXTURE_IP, FIXTURE_MAC, boot_unit_facts, capacity_facts, cloud_status_facts, diagnostic_command,
    fixture_ipv4_facts,
)
from test_stock_kexec_packets import EXPECTED, SNAPLEN, packet_facts
from test_rehearsal_lease import RUN, write_reviewed_input


class RescueInstallTest(unittest.TestCase):
    def setUp(self):
        self.private = tempfile.TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.directory = Path(self.private.name)

    def write(self, name, data):
        path = self.directory / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def test_binding_rejects_stale_baseline_or_wrong_role(self):
        run = "a" * 32
        self.write("lease.json", {"schema": 1, "run": run, "expires": 9000,
                                  "baseline": {"servers": {"41": {}}}})
        self.write("original-created.json", {"servers": [41, 42]})
        binding = {"run": run, "phase": "original", "role": "app", "serverId": 42,
                   "ipv4": "1.1.1.1", "expires": 8000, "observedAt": 1000}
        self.write("original-app-binding-1.json", binding)
        self.assertEqual(rescue.lease_binding(self.directory, "original", "app", 1100)["serverId"], 42)
        for changes, now in [({}, 1121), ({"serverId": 41}, 1100), ({"serverId": 43}, 1100),
                             ({"role": "relay"}, 1100), ({"expires": 1099}, 1100),
                             ({"ipv4": "127.0.0.1"}, 1100)]:
            with self.subTest(changes=changes, now=now):
                self.write("original-app-binding-10.json", {**binding, **changes})
                with self.assertRaises(rescue.Rejected):
                    rescue.lease_binding(self.directory, "original", "app", now)

    def test_closed_receipt_blocks_stage_even_with_valid_lease(self):
        run = "a" * 32
        self.write("lease.json", {"schema": 1, "run": run, "expires": 9000})
        with patch.object(rescue.time, "time", return_value=1000):
            rescue.require_open(self.directory, run, 8000)
            self.assertEqual(rescue.remaining(self.directory, {"run": run, "expires": 1500}, 1800), 480)
            with self.assertRaises(rescue.Rejected):
                rescue.require_open(self.directory, run, 999)
            self.write("closed.json", {})
            with self.assertRaises(rescue.Rejected):
                rescue.require_open(self.directory, run, 8000)

    def test_rescue_pin_must_match_console_and_exact_host(self):
        blob = rescue.KEY_PREFIX + b"x" * 32
        encoded = base64.b64encode(blob)
        fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
        known = b"1.1.1.1 ssh-ed25519 " + encoded + b"\n"
        self.assertEqual(rescue.known_key(known, "1.1.1.1", fingerprint), encoded.decode())
        for address, expected in [("8.8.8.8", fingerprint), ("1.1.1.1", b"SHA256:wrong")]:
            with self.assertRaises(rescue.Rejected):
                rescue.known_key(known, address, expected)

    def test_stock_pin_must_match_the_proven_key_before_ssh(self):
        blob = rescue.KEY_PREFIX + b"x" * 32
        encoded = base64.b64encode(blob).decode()
        fingerprint = b"SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).rstrip(b"=")
        for name, contents in [
            ("original-relay-bootstrap-known-hosts", f"1.1.1.1 ssh-ed25519 {encoded}\n".encode()),
            ("original-relay-bootstrap-fingerprint", fingerprint),
            ("operator-ed25519", b"fixture"),
        ]:
            path = self.directory / name
            path.write_bytes(contents)
            path.chmod(0o600)
        proof = {"run": RUN, "serverId": 7, "ipv4": "1.1.1.1", "publicKey": "ssh-ed25519 " + encoded,
                 "nonceSha256": "d" * 64, "fingerprint": fingerprint.decode()}
        rescue.write_private(self.directory / "original-relay-bootstrap-proof.json", proof)
        binding = {"ipv4": "1.1.1.1", "serverId": 7}
        with patch.object(rescue, "lease_binding", return_value=binding), patch.object(
            rescue, "reviewed_bootstrap", return_value=(RUN, "d" * 64)
        ) as reviewed:
            _, key, options, ssh = rescue.bootstrap_authority(self.directory, "original", "relay")
            self.assertEqual(key, encoded)
            self.assertIn("StrictHostKeyChecking=yes", options)
            self.assertTrue(any(x.endswith("original-relay-bootstrap-known-hosts") for x in options))
            self.assertEqual(ssh[-1], "root@1.1.1.1")
            reviewed.return_value = (RUN, "e" * 64)
            with self.assertRaisesRegex(rescue.Rejected, "proven host key"):
                rescue.bootstrap_authority(self.directory, "original", "relay")
            reviewed.return_value = (RUN, "d" * 64)
            binding["serverId"] = 8
            with self.assertRaisesRegex(rescue.Rejected, "proven host key"):
                rescue.bootstrap_authority(self.directory, "original", "relay")

    SAFE_SESSION = ("BatchMode=yes", "StrictHostKeyChecking=accept-new", "GlobalKnownHostsFile=/dev/null",
                    "HostKeyAlgorithms=ssh-ed25519", "UpdateHostKeys=no", "CheckHostIP=no", "HashKnownHosts=no",
                    "ControlMaster=no", "PreferredAuthentications=publickey", "PubkeyAuthentication=no",
                    "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "GSSAPIAuthentication=no",
                    "HostbasedAuthentication=no", "IdentitiesOnly=yes", "IdentityAgent=none", "ForwardAgent=no",
                    "ClearAllForwardings=yes")

    def fake_session(self, key, banners, *, known_text=None, stderr_extra=b""):
        """Simulate one SSH session that records `key` and prints `banners` on stderr."""
        def run(argv, **_kwargs):
            known = next(Path(x.split("=", 1)[1]) for x in argv if x.startswith("UserKnownHostsFile="))
            if known_text is not None:
                known.write_text(known_text)
            elif key is not None:
                known.write_text(f"1.1.1.1 ssh-ed25519 {key}\n")
            self.assertEqual(argv[:3], ["ssh", "-F", "/dev/null"])
            self.assertEqual(argv[-1], "true")
            for option in self.SAFE_SESSION:
                self.assertIn(option, argv)
            self.assertFalse(any(x == "-i" or x.startswith("IdentityFile") for x in argv))
            stderr = b"Warning: Permanently added '1.1.1.1' (ED25519) to the list of known hosts.\r\n"
            stderr += b"".join(b"terrace-hostkey-proof v1 " + mac.encode() + b"\r\n" for mac in banners)
            return subprocess.CompletedProcess(argv, 255, b"", stderr + stderr_extra
                                               + b"root@1.1.1.1: Permission denied (publickey).\r\n")
        return patch.object(rescue.subprocess, "run", side_effect=run)

    def test_host_key_proof_session_rejects_odd_records_and_bounds_output(self):
        nonce = "0123456789abcdef" * 4
        context = (RUN, "original", "relay")
        genuine = base64.b64encode(rescue.KEY_PREFIX + b"g" * 32).decode()
        mac = rescue.proof_mac(nonce, *context, "ssh-ed25519 " + genuine)
        for text in (f"1.1.1.1 ssh-ed25519 {genuine}\n1.1.1.1 ssh-ed25519 {genuine}\n",
                     f"1.1.1.1 ssh-rsa {genuine}\n",
                     "1.1.1.1 ssh-ed25519 " + base64.b64encode(b"x" * 51).decode() + "\n"):
            with self.fake_session(genuine, [mac], known_text=text), self.assertRaises(rescue.Rejected):
                rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5)
        with self.fake_session(genuine, [mac], stderr_extra=b"x" * rescue.PROOF_OUTPUT_LIMIT):
            self.assertIsNone(rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5))
        with patch.object(rescue.subprocess, "run", side_effect=subprocess.TimeoutExpired(["ssh"], 5)):
            self.assertIsNone(rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5))

    def test_proof_nonce_generation_and_entropy(self):
        path = self.directory / "original-relay-bootstrap-nonce"
        digest = rescue.write_proof_nonce(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(rescue.proof_nonce(self.directory, "original-relay", digest), path.read_text().strip())
        with self.assertRaises(FileExistsError):
            rescue.write_proof_nonce(path)
        self.assertFalse(rescue.strong_nonce("a" * 64))
        self.assertFalse(rescue.strong_nonce("ab" * 32))
        weak = self.directory / "replacement-relay-bootstrap-nonce"
        weak.write_text("ab" * 32 + "\n")
        weak.chmod(0o600)
        with self.assertRaisesRegex(rescue.Rejected, "nonce differs"):
            rescue.proof_nonce(self.directory, "replacement-relay", hashlib.sha256(weak.read_bytes()).hexdigest())

    def test_host_key_proof_binds_nonce_context_and_presented_key(self):
        nonce = "0123456789abcdef" * 4
        context = (RUN, "original", "relay")
        genuine = base64.b64encode(rescue.KEY_PREFIX + b"g" * 32).decode()
        attacker = base64.b64encode(rescue.KEY_PREFIX + b"a" * 32).decode()
        mac = rescue.proof_mac(nonce, *context, "ssh-ed25519 " + genuine)
        with self.fake_session(genuine, [mac]):
            self.assertEqual(rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5), genuine)
        # A relayed genuine banner does not vouch for a substituted key.
        with self.fake_session(attacker, [mac]), self.assertRaisesRegex(rescue.Rejected, "proof rejected"):
            rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5)
        for wrong in (rescue.proof_mac("f" * 64, *context, "ssh-ed25519 " + genuine),
                      rescue.proof_mac(nonce, RUN, "replacement", "relay", "ssh-ed25519 " + genuine)):
            with self.fake_session(genuine, [wrong]), self.assertRaisesRegex(rescue.Rejected, "proof rejected"):
                rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5)
        with self.fake_session(genuine, [mac, mac]), self.assertRaisesRegex(rescue.Rejected, "proof rejected"):
            rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5)
        # Not yet published: retry later rather than trust anything.
        for key, banners in ((genuine, []), (None, [])):
            with self.fake_session(key, banners):
                self.assertIsNone(rescue.verified_host_key(nonce, context, "root@1.1.1.1", [], 5))

    def test_proof_pins_exactly_once_from_the_reviewed_nonce(self):
        nonce = "0123456789abcdef" * 4
        digest = hashlib.sha256((nonce + "\n").encode()).hexdigest()
        path = self.directory / "original-relay-bootstrap-nonce"
        path.write_text(nonce + "\n")
        path.chmod(0o600)
        genuine = base64.b64encode(rescue.KEY_PREFIX + b"g" * 32).decode()
        mac = rescue.proof_mac(nonce, RUN, "original", "relay", "ssh-ed25519 " + genuine)
        binding = {"ipv4": "1.1.1.1", "serverId": 7}
        with patch.object(rescue, "lease_binding", return_value=binding), patch.object(
            rescue, "reviewed_bootstrap", return_value=(RUN, digest)
        ), patch.object(rescue, "remaining", return_value=20), patch.object(rescue.time, "sleep"):
            with self.fake_session(genuine, []), self.assertRaisesRegex(rescue.Rejected, "not published in time"):
                rescue.prove_host_key(self.directory, "original", "relay", seconds=1, delay=5)
            self.assertFalse((self.directory / "original-relay-bootstrap-known-hosts").exists())
            with self.fake_session(genuine, [mac]):
                self.assertEqual(rescue.prove_host_key(self.directory, "original", "relay"), genuine)
            self.assertEqual((self.directory / "original-relay-bootstrap-known-hosts").read_text(),
                             f"1.1.1.1 ssh-ed25519 {genuine}\n")
            with self.fake_session(genuine, [mac]), self.assertRaisesRegex(rescue.Rejected, "already pinned"):
                rescue.prove_host_key(self.directory, "original", "relay")
        for name in ("known-hosts", "fingerprint", "proof.json"):
            (self.directory / f"original-relay-bootstrap-{name}").unlink()
        attacker = base64.b64encode(rescue.KEY_PREFIX + b"a" * 32).decode()
        bindings = [{"ipv4": "1.1.1.1", "serverId": 7}, {"ipv4": "1.1.1.1", "serverId": 8}]
        with patch.object(rescue, "reviewed_bootstrap", return_value=(RUN, digest)), patch.object(
            rescue, "remaining", return_value=20
        ), patch.object(rescue.time, "sleep") as sleep:
            # A wrong proof aborts at once instead of retrying until a forgery fits.
            with patch.object(rescue, "lease_binding", return_value=bindings[0]), self.fake_session(
                attacker, [mac]
            ), self.assertRaisesRegex(rescue.Rejected, "proof rejected"):
                rescue.prove_host_key(self.directory, "original", "relay")
            sleep.assert_not_called()
            with patch.object(rescue, "lease_binding", side_effect=bindings), self.fake_session(
                genuine, [mac]
            ), self.assertRaisesRegex(rescue.Rejected, "binding changed"):
                rescue.prove_host_key(self.directory, "original", "relay")
        self.assertFalse((self.directory / "original-relay-bootstrap-known-hosts").exists())
        with patch.object(rescue, "reviewed_bootstrap", return_value=(RUN, "0" * 64)):
            with self.assertRaisesRegex(rescue.Rejected, "nonce differs"):
                rescue.proof_nonce(self.directory, "original-relay", "0" * 64)

    def test_stock_published_key_accepts_one_line_with_optional_comment(self):
        path = self.directory / "host.pub"
        lines = stock_kexec.STOCK_PROBE.splitlines()
        start = next(index for index, line in enumerate(lines) if line.startswith("mapfile -t -n 2 published "))
        end = next(index for index, line in enumerate(lines) if line.startswith("auth="))
        script = 'set -euo pipefail\nkeys=(unused "$2")\n' + "\n".join(lines[start:end])
        key = b"ssh-ed25519 synthetic"
        for data, expected in [(key, 0), (key + b"\n", 0), (key + b"\n\n", 1),
                               (key + b" root@debian\n", 0), (key + b"\nssh-ed25519 other\n", 1),
                               (b"ssh-ed25519 other root@debian\n", 1), (b"ssh-rsa synthetic\n", 1),
                               (b"ssh-ed25519 synthetic2\n", 1), (b"", 1)]:
            path.write_bytes(data)
            result = subprocess.run(["bash", "-c", script, "fixture", "synthetic", str(path)], timeout=5)
            self.assertEqual(result.returncode, expected)

    def stock_segment(self, first, last):
        lines = stock_kexec.STOCK_PROBE.splitlines()
        start = lines.index(first)
        end = next(index for index in range(start, len(lines)) if lines[index] == last)
        return "\n".join(lines[start:end + 1])

    def run_stock(self, body, *args, fake=None):
        # The real header (strict mode and the failure trap) around one part of the probe.
        header = "\n".join(stock_kexec.STOCK_PROBE.splitlines()[1:3])
        env = None
        if fake:
            bin_dir = self.directory / "bin"
            bin_dir.mkdir(exist_ok=True)
            for name, text in fake.items():
                path = bin_dir / name
                path.write_text("#!/bin/sh\n" + text)
                path.chmod(0o700)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        result = subprocess.run(["bash", "-c", header + "\n" + body + "\necho passed", "probe", *args],
                                capture_output=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0)
        if result.stdout == b"passed\n":
            return True
        self.assertRegex(result.stdout, rb"^stock-check-failed [1-9][0-9]{0,2} [1-9][0-9]{0,2}\n$")
        return False

    def test_stock_probe_reports_only_the_failed_line_and_status(self):
        lines = stock_kexec.STOCK_PROBE.splitlines()
        result = subprocess.run(["bash", "-c", stock_kexec.STOCK_PROBE, "probe", "host", "operator"],
                                capture_output=True, timeout=10,
                                env={**os.environ, "PATH": f"{self.directory}:{os.environ['PATH']}"}
                                if self.fake_id() else None)
        expected = lines.index('test "$(id -u)" = 0') + 1
        self.assertEqual((result.returncode, result.stdout), (0, f"stock-check-failed {expected} 1\n".encode()))
        with patch.object(stock_kexec.install, "remote", return_value=result.stdout):
            with self.assertRaises(stock_kexec.StockCheckFailed) as caught:
                stock_kexec.stock_check(["ssh"], "host", "operator")
        self.assertEqual((caught.exception.line, caught.exception.status), (expected, 1))

    def fake_id(self):
        path = self.directory / "id"
        path.write_text("#!/bin/sh\necho 1000\n")
        path.chmod(0o700)
        return True

    def test_stock_check_accepts_only_a_boot_id_or_an_exact_report(self):
        boot = b"abcd0123-1234-1234-1234-1234567890ab\n"
        with patch.object(stock_kexec.install, "remote", return_value=boot):
            self.assertEqual(stock_kexec.stock_check(["ssh"], "host", "operator"), boot.decode().strip())
        for output in [b"stock-check-failed 12 1\nextra\n", b"stock-check-failed 0 1\n",
                       b"stock-check-failed 12 1", b"stock-check-failed 1234 1\n", b"x" + boot]:
            with patch.object(stock_kexec.install, "remote", return_value=output):
                with self.assertRaises(rescue.Rejected) as caught:
                    stock_kexec.stock_check(["ssh"], "host", "operator")
                self.assertNotIsInstance(caught.exception, stock_kexec.StockCheckFailed)

    def test_stock_failure_message_carries_only_line_and_status(self):
        argv = ["stock_kexec.py", str(self.directory), "original", "relay", "--dispatch", "a", "b"]
        with patch.object(sys, "argv", argv), patch.object(stock_kexec, "dispatch",
                                                           side_effect=stock_kexec.StockCheckFailed(41, 1)):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(stock_kexec.main(), 1)
        self.assertEqual(stderr.getvalue(),
                         "Stock host check failed at line 41 (status 1). Private inputs suppressed.\n")

    def test_stock_probe_checks_are_never_hidden_in_and_lists(self):
        for script in [rescue.PROBE, rescue.BOOTSTRAP, rescue.FORMAT, rescue.FINISH, rescue.RAM_PROBE,
                       rescue.RAM_SERVE, stock_kexec.STOCK_PROBE, stock_kexec.UPLOAD, stock_kexec.KEXEC]:
            for line in script.splitlines():
                if "||" not in line:  # A list that handles its own failure, like UPLOAD's cleanup.
                    self.assertNotRegex(line, r"\btest\b[^|&]*&&")
        for line in stock_kexec.STOCK_PROBE.splitlines()[3:]:
            self.assertNotIn("&&", line)
            if "||" in line:
                self.assertIn(line.strip(), {"cloud-init status --wait >/dev/null || cloud_init=$?",
                                             'cloud_status="$(cloud-init status --format json)" || test "$?" = 2'})

    def test_stock_cloud_init_allows_only_finished_degraded_runs(self):
        body = self.stock_segment("cloud_init=0", "fi")
        good = json.dumps({"status": "done", "extended_status": "degraded done", "errors": [],
                           "recoverable_errors": {"WARNING": ["datasource"]}})
        fake = ('case "$*" in\n"status --wait") exit "$WAIT";;\n'
                '"status --format json") printf "%s\\n" "$STATUS"; exit "$JSON";;\nesac\nexit 9\n')
        cases = [("0", "{}", "0", True), ("2", good, "2", True),
                 ("1", good, "1", False), ("3", good, "2", False), ("2", good, "1", False),
                 ("2", good.replace("degraded done", "done"), "2", False),
                 ("2", good.replace('"done"', '"error"', 1), "2", False),
                 ("2", good.replace('"errors": []', '"errors": ["x"]'), "2", False),
                 ("2", "not json", "2", False), ("2", "{}", "2", False),
                 ("2", good.replace('["datasource"]', '["Running module ssh (cc_ssh) failed"]'), "2", False),
                 ("2", good.replace('{"WARNING": ["datasource"]}', '[]'), "2", False)]
        for wait, status, json_rc, expected in cases:
            with self.subTest(wait=wait, status=status, json_rc=json_rc):
                with patch.dict("os.environ", {"WAIT": wait, "STATUS": status, "JSON": json_rc}):
                    self.assertIs(self.run_stock(body, fake={"cloud-init": fake}), expected)

    def test_stock_hostkeys_allow_only_absent_extra_paths(self):
        body = 'settings="$(cat "$1")"\n' + self.stock_segment(
            'hostkeys="$(grep \'^hostkey \' <<<"$settings")"', 'done <<<"$hostkeys"')
        present = self.directory / "present_key"
        present.write_text("x")
        dangling = self.directory / "dangling_key"
        dangling.symlink_to(self.directory / "missing")
        ours = "hostkey /etc/ssh/ssh_host_ed25519_key"
        cases = [(ours, True), (f"{ours}\nhostkey /nonexistent/ssh_host_rsa_key", True),
                 (f"hostkey /nonexistent/ssh_host_rsa_key\n{ours}", True),
                 (f"{ours}\nhostkey {present}", False), (f"{ours}\nhostkey {dangling}", False),
                 ("hostkey /nonexistent/ssh_host_rsa_key", False), ("port 22", False),
                 (f"{ours}_extra", False), (f"{ours}\nhostkey {present} ", False),
                 (f"{ours}\nhostkey relative_key", False), (f"{ours}\nhostkey /no such/key", False),
                 (f"{ours}\nhostkey /nonexistent/*", False)]
        for text, expected in cases:
            with self.subTest(text=text):
                settings = self.directory / "settings"
                settings.write_text("port 22\n" + text + "\n")
                self.assertIs(self.run_stock(body, str(settings)), expected)

    def test_stock_sshd_settings_allow_no_other_login_route(self):
        lines = stock_kexec.STOCK_PROBE.splitlines()
        start = lines.index("# No login route besides the authorized_keys files checked below.") + 1
        end = lines.index("fi", start)
        body = 'settings="$(cat "$1")"\n' + "\n".join(lines[start:end + 1])
        base = ["authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2", "authorizedkeyscommand none",
                "authorizedprincipalscommand none", "authorizedprincipalsfile none", "trustedusercakeys none",
                "hostkeyagent none"]
        cases = [(base, True), (["authorizedkeysfile .ssh/authorized_keys", *base[1:]], True),
                 (["authorizedkeysfile .ssh/authorized_keys /etc/ssh/keys/%u", *base[1:]], False),
                 ([*base, "hostcertificate /etc/ssh/cert.pub"], False)]
        for index in range(1, len(base)):
            changed = list(base)
            changed[index] = changed[index].replace(" none", " /usr/bin/true")
            cases.append((changed, False))
        for settings_lines, expected in cases:
            with self.subTest(settings=settings_lines):
                settings = self.directory / "settings"
                settings.write_text("\n".join(["port 22", *settings_lines]) + "\n")
                self.assertIs(self.run_stock(body, str(settings)), expected)

    def test_stock_requires_uefi_without_lockdown(self):
        body = self.stock_segment("test -d /sys/firmware/efi", "fi")
        efi = self.directory / "efi"
        lockdown = self.directory / "lockdown"
        body = body.replace("/sys/firmware/efi", str(efi)).replace("/sys/kernel/security/lockdown", str(lockdown))
        cases = [(True, None, True), (False, None, False), (True, "[none] integrity confidentiality", True),
                 (True, "none [integrity] confidentiality", False), (True, "none integrity [confidentiality]", False)]
        for has_efi, mode, expected in cases:
            with self.subTest(efi=has_efi, lockdown=mode):
                if has_efi:
                    efi.mkdir(exist_ok=True)
                elif efi.exists():
                    efi.rmdir()
                if mode is None:
                    lockdown.unlink(missing_ok=True)
                else:
                    lockdown.write_text(mode + "\n")
                self.assertIs(self.run_stock(body), expected)

    def test_install_scripts_require_uefi_and_a_fallback_boot_path(self):
        self.assertIn("\ntest -d /sys/firmware/efi\n", rescue.PROBE)
        for script in (rescue.PROBE, stock_kexec.STOCK_PROBE):
            self.assertNotIn("! -e /sys/firmware/efi", script)
        for script in (rescue.FORMAT, rescue.FINISH):
            self.assertIn("--mountpoint /mnt/boot)", script)
            self.assertIn('test "$esp_kind" = vfat', script)
            self.assertIn("= c12a7328-f81f-11d2-ba4b-00a0c93ec93b", script)
        install_line = rescue.FINISH.index("nixos-install")
        for check in ("test -f /mnt/boot/EFI/BOOT/BOOTX64.EFI", "test ! -L /mnt/boot/EFI/BOOT/BOOTX64.EFI",
                      "test -f /mnt/boot/loader/loader.conf"):
            self.assertGreater(rescue.FINISH.index(check), install_line)

    def test_stock_authorized_keys_allow_only_repeats_of_the_operator(self):
        body = 'auth="$1"\n' + self.stock_segment("mapfile -t -n 5 lines < \"$auth\"", "done")
        line = "ssh-ed25519 operator"
        cases = [(line + "\n", True), (line, True), (f"{line} terrace\n{line} hetzner\n", True),
                 ((line + "\n") * 4, True), ((line + "\n") * 5, False), ("", False),
                 (f"{line}\n\n", False), (f"{line}\nssh-ed25519 other\n", False),
                 (f'command="true" {line}\n', False), (f"{line}\n# note\n", False),
                 ("ssh-rsa operator\n", False), ("ssh-ed25519 operator2\n", False),
                 (f"{line}\r\n", False), ("ssh-ed25519\toperator\n", True), (f"  {line}\n", True)]
        for text, expected in cases:
            with self.subTest(text=text):
                auth = self.directory / "authorized_keys"
                auth.write_text(text)
                self.assertIs(self.run_stock(body, str(auth), "operator"), expected)

    def test_stock_dispatch_checks_archive_and_rebinds_before_kexec(self):
        archive = self.directory / "installer.tar.gz"
        self.stock_archive(archive)
        digest = rescue.sha(archive.read_bytes())
        binding = {"run": RUN, "ipv4": "1.1.1.1"}
        boot = "abcd0123-1234-1234-1234-1234567890ab"
        other = {**binding, "ipv4": "8.8.8.8"}
        initial = (binding, "fixture", [], ["ssh"])
        with patch.object(rescue, "bootstrap_authority", return_value=initial) as authority, patch.object(
            stock_kexec, "stock_boot", return_value=boot
        ), patch.object(rescue, "remaining", return_value=60), patch.object(rescue, "remote") as remote:
            with self.assertRaisesRegex(rescue.Rejected, "archive rejected"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, "0" * 64)
            remote.assert_not_called()
            authority.side_effect = [initial, (other, "fixture", [], ["ssh"])]
            with self.assertRaisesRegex(rescue.Rejected, "target changed"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)
            self.assertEqual(remote.call_count, 1)
            self.assertEqual(remote.call_args.args[2], stock_kexec.UPLOAD)
            authority.side_effect = None
            stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)
            self.assertEqual(remote.call_args.args[2], stock_kexec.KEXEC)
            sent, _ = rescue.read_json(self.directory / "original-relay-kexec-dispatched.json")
            self.assertEqual(sent["archiveUnpackedBytes"], len(stock_kexec.ARCHIVE_FILES))
            with self.assertRaisesRegex(rescue.Rejected, "already dispatched"):
                stock_kexec.dispatch(self.directory, "original", "relay", archive, digest)

    def test_runner_copies_a_readable_machine_id(self):
        # Only the upstream runner runs under 022, after the 0700 scratch checks.
        self.assertIn("umask 077\n", stock_kexec.UPLOAD)
        lines = stock_kexec.KEXEC.strip().splitlines()
        self.assertEqual(lines[1], "umask 077")
        self.assertEqual(lines[-2:], ["umask 022", "exec setsid --wait timeout --kill-after=5 90 ./kexec/run"])

    def stock_archive(self, path, names=None):
        with tarfile.open(path, "w:gz") as archive:
            root = tarfile.TarInfo("kexec")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for name in sorted(stock_kexec.ARCHIVE_FILES) if names is None else names:
                entry = tarfile.TarInfo(name)
                entry.size = 1
                archive.addfile(entry, io.BytesIO(b"x"))

    def test_stock_archive_requires_exact_entries_and_measures_unpacked_size(self):
        path = self.directory / "installer.tar.gz"
        self.stock_archive(path)
        with path.open("rb") as stream:
            self.assertEqual(stock_kexec.archive_unpacked_bytes(stream), 5)
        for extra in ("kexec/run", "kexec/extra", "../escaping"):
            self.stock_archive(path, [*stock_kexec.ARCHIVE_FILES, extra])
            with path.open("rb") as stream, self.assertRaisesRegex(rescue.Rejected, "unexpected installer"):
                stock_kexec.archive_unpacked_bytes(stream)
        self.stock_archive(path, ["kexec/run"])
        with path.open("rb") as stream, self.assertRaisesRegex(rescue.Rejected, "entry missing"):
            stock_kexec.archive_unpacked_bytes(stream)

    def test_native_diagnostic_pipes_are_capped_and_timed(self):
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], 5
        ), (0, b"out\n", b"err\n"))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "print('x' * 8192)"], 5
        ), (125, b"", b""))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "print('x' * 8192)"], 5, truncate=True
        ), (0, b"x" * 8192, b""))
        self.assertEqual(diagnostic_command(
            [sys.executable, "-c", "import time; time.sleep(10)"], 0.05
        ), (124, b"", b""))
        with tempfile.TemporaryFile() as data:
            data.write(b"exact synthetic input")
            data.seek(0)
            self.assertEqual(diagnostic_command(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                5, stdin=data,
            ), (0, b"exact synthetic input", b""))

    def test_cloud_status_keeps_only_known_states_and_counts(self):
        status = {"status": "done", "extended_status": "degraded done", "errors": [],
                  "recoverable_errors": {"WARNING": ["synthetic private message"]}}
        self.assertEqual(cloud_status_facts(2, json.dumps(status)), {
            "statusReadRc": 2, "status": "done", "extendedStatus": "degraded done",
            "fatalErrorCount": 0, "recoverableErrorCount": 1,
        })
        self.assertIsNone(cloud_status_facts(1, b'{"status":"synthetic private message"}')["status"])
        self.assertIsNone(cloud_status_facts(125, b"")["recoverableErrorCount"])

    def test_upload_capacity_diagnostics_keep_only_flags_and_numbers(self):
        self.assertEqual(capacity_facts(b"tmpfs\nfixture-type\n12\n34\n56\n"), {
            "runTmpfs": True, "shmTmpfs": False, "runFreeBytes": 12,
            "shmFreeBytes": 34, "memAvailableKiB": 56,
        })
        self.assertIsNone(capacity_facts(b"tmpfs\ntmpfs\nraw-private\n34\n56\n"))

    def test_upload_failure_cleanup_targets_its_exact_leaf(self):
        leaf = self.directory / "owned-upload"
        leaf.mkdir()
        cleanup = "cleanup() {" + stock_kexec.UPLOAD.split("cleanup() {", 1)[1].split("trap cleanup", 1)[0]
        script = """
set -euo pipefail
scratch=$1
mountpoint() { test "$3" = "$scratch"; }
findmnt() { test "${@: -1}" = "$scratch"; printf 'tmpfs\\n'; }
umount() { test "$2" = "$scratch"; printf '%s\\n' "$2"; }
""" + cleanup + 'trap cleanup EXIT\ncd "$scratch"\nexit 7'
        result = subprocess.run(["bash", "-c", script, "fixture", str(leaf)], capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (7, (str(leaf) + "\n").encode()))
        self.assertFalse(leaf.exists())

    def test_boot_diagnostics_classify_only_known_units_and_errors(self):
        raw = (b"\x1b[32m[ OK ]\x1b[0m Started SSH Daemon.\r\n"
               b"Starting restore-state-from-initrd.service...\n"
               b"systemd-networkd.service: Failed with result 'fixture-private'.\n"
               b"sshd: Bind to port 22 complete.\n"
               b"DHCPv4 request timed out.\n")
        facts = boot_unit_facts(raw)
        self.assertEqual(facts["units"]["sshd"], {"begin": False, "success": True, "failure": False})
        self.assertTrue(facts["units"]["restoreState"]["begin"])
        self.assertTrue(facts["units"]["networkd"]["failure"])
        self.assertFalse(facts["sshBindError"])
        self.assertTrue(facts["dhcpError"])
        self.assertNotIn("fixture-private", json.dumps(facts))

    def test_packet_diagnostics_separate_leases_and_late_ssh_headers(self):
        other = bytes([10, 0, 2, 16])

        def ipv4(protocol, source, destination, payload):
            header = bytearray(20)
            header[0], header[9] = 0x45, protocol
            header[12:16], header[16:20] = source, destination
            return bytes(12) + b"\x08\x00" + header + payload

        def dhcp(message, lease, reply):
            data = bytearray(240)
            data[16:20], data[236:240] = lease, b"\x63\x82\x53\x63"
            ports = (67, 68) if reply else (68, 67)
            return ipv4(17, other, EXPECTED,
                        struct.pack(">HHHH", *ports, 252, 0) + data + bytes([53, 1, message, 255]))

        def tcp(source_port, target_port, flags, payload=b""):
            header = bytearray(20)
            struct.pack_into(">HH", header, 0, source_port, target_port)
            header[12:14] = bytes([0x50, flags])
            source, destination = (EXPECTED, other) if source_port == 22 else (other, EXPECTED)
            return ipv4(6, source, destination, header + payload)

        def record(frame):
            return struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame

        initial = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, SNAPLEN, 1)
        initial += b"".join(map(record, [dhcp(3, EXPECTED, False), dhcp(5, EXPECTED, True),
                                        dhcp(5, other, True), tcp(22, 1234, 0x12)]))
        late = b"".join(map(record, [tcp(1234, 22, 0x02), tcp(22, 1234, 0x12),
                                     tcp(22, 1234, 0x18, b"SSH-2.0-synthetic")]))
        facts = packet_facts(initial + late, len(initial))
        for key in ("dhcpRequest", "dhcpAckExpected", "dhcpAckAlternate", "lateTcpSynToExpected",
                    "lateTcpSynAckFromExpected", "lateBannerFromExpected"):
            self.assertEqual(facts[key], 1)
        self.assertTrue(facts["completeRecords"])
        self.assertNotIn("synthetic", json.dumps(facts))

    def test_fixture_ipv4_matches_mac_and_requires_static_stock_address(self):
        address = {"family": "inet", "local": FIXTURE_IP, "prefixlen": 24}
        ipv6 = {"family": "inet6", "local": "fe80::1", "prefixlen": 64}
        interface = {"address": FIXTURE_MAC, "addr_info": [address, ipv6]}
        self.assertFalse(fixture_ipv4_facts(json.dumps([{"addr_info": [address]}]), static=True))
        self.assertTrue(fixture_ipv4_facts(json.dumps([interface]), static=True))
        address["dynamic"] = True
        self.assertFalse(fixture_ipv4_facts(json.dumps([interface]), static=True))
        self.assertTrue(fixture_ipv4_facts(json.dumps([interface]), static=False))
        self.assertFalse(fixture_ipv4_facts(json.dumps([interface, interface]), static=False))
        interface["addr_info"].append(dict(address, local="10.0.2.16"))
        self.assertFalse(fixture_ipv4_facts(json.dumps([interface]), static=False))

    def test_ram_receipt_binds_new_boot_and_stock_dispatch(self):
        old = "abcd0123-1234-1234-1234-1234567890ab"
        new = "abcd0123-1234-1234-1234-1234567890cd"
        binding = {"run": RUN, "ipv4": "1.1.1.1"}
        dispatch = self.write("original-relay-kexec-dispatched.json", {
            "binding": binding, "bootstrapKey": "fixture", "stockBootId": old,
        })
        value = (binding, "fixture", [], ["ssh"])
        with patch.object(rescue, "bootstrap_authority", return_value=value), patch.object(
            rescue, "ram_boot_id", return_value=old
        ) as boot:
            with self.assertRaisesRegex(rescue.Rejected, "stock boot is still running"):
                stock_kexec.verify_ram(self.directory, "original", "relay")
            boot.return_value = new
            stock_kexec.verify_ram(self.directory, "original", "relay")
            self.assertEqual(rescue.authority(self.directory, "original", "relay", ram=True), value)
            boot.return_value = old
            with self.assertRaisesRegex(rescue.Rejected, "RAM installer changed"):
                rescue.authority(self.directory, "original", "relay", ram=True)
            boot.return_value = new
            dispatch.write_bytes(dispatch.read_bytes() + b" ")
            with self.assertRaisesRegex(rescue.Rejected, "RAM installer changed"):
                rescue.authority(self.directory, "original", "relay", ram=True)

    def test_console_probe_cannot_dispatch_an_install(self):
        cfg = {"run": RUN, "phase": "original", "probeOnly": True}
        path = write_reviewed_input(self.directory, cfg)
        with patch.object(rescue, "authority") as authority:
            with self.assertRaisesRegex(rescue.Rejected, "cannot write a disk"):
                rescue.install(self.directory, "original", "relay", "unused", self.directory / "archive")
            path.unlink()
            self.write("original-input.json", {"run": RUN, "phase": "original"})
            with self.assertRaisesRegex(rescue.Rejected, "no longer matches"):
                rescue.install(self.directory, "original", "relay", "unused", self.directory / "archive")
            authority.assert_not_called()

    def test_disk_probe_rejects_mount_holder_and_partition(self):
        row = {"path": "/dev/vda", "type": "disk", "size": 123456, "ro": False,
               "rm": False, "maj:min": "252:0", "mountpoints": [None]}
        part = {**row, "path": "/dev/vda1", "type": "part"}

        def facts(disk):
            return ("abcd0123-1234-1234-1234-1234567890ab\n/dev/vda\n"
                    + json.dumps({"blockdevices": [disk]})).encode()

        alias = "/dev/disk/by-id/scsi-test"
        self.assertEqual(rescue.parse_probe(facts({**row, "children": [part]}), alias)["canonical"], "/dev/vda")
        for changed in [{**row, "children": [{**part, "mountpoints": ["/mnt"]}]},
                        {**row, "children": [{**part, "children": [row]}]},
                        {**row, "type": "part"}, {**row, "rm": True}]:
            with self.assertRaises(rescue.Rejected):
                rescue.parse_probe(facts(changed), alias)
        for bad in ["/dev/vda1", "/dev/disk/by-id/scsi-test-part1", "/dev/../dev/vda", "/dev/nvme0n1p1"]:
            self.assertFalse(rescue.valid_disk(bad))

    def test_approved_plan_must_match_disk_and_exact_bytes(self):
        store = "/nix/store/" + "a" * 32 + "-"
        plan = {"diskSha256": "b" * 64, "disko": store + "disko",
                "installer": store + "installer", "system": store + "system"}
        path = self.write("original-app-install.json", plan)
        digest = rescue.sha(path.read_bytes())
        with patch.object(Path, "exists", return_value=True):
            self.assertEqual(rescue.checked_plan(self.directory, "original-app", digest, plan["diskSha256"]), plan)
            with self.assertRaises(rescue.Rejected):
                rescue.checked_plan(self.directory, "original-app", digest, "c" * 64)
            self.write(path.name, {**plan, "system": store + "changed"})
            with self.assertRaises(rescue.Rejected):
                rescue.checked_plan(self.directory, "original-app", digest, plan["diskSha256"])

    def test_copy_targets_mounted_store_with_strict_ssh_options(self):
        options = ["-o", "StrictHostKeyChecking=yes"]
        with patch.object(rescue, "checked_process") as checked:
            rescue.copy_store({"ipv4": "1.1.1.1"}, options, 10, ["/nix/store/reviewed"], target=True)
        args, _seconds = checked.call_args.args
        self.assertIn("remote-store=local%3Froot%3D%2Fmnt", args[3])
        self.assertEqual(checked.call_args.kwargs["env"]["NIX_SSHOPTS"], "-o StrictHostKeyChecking=yes")

    def test_all_fixed_remote_scripts_parse_without_execution(self):
        for script in [rescue.PROBE, rescue.BOOTSTRAP, rescue.FORMAT, rescue.FINISH,
                       rescue.RAM_PROBE, rescue.RAM_SERVE,
                       stock_kexec.STOCK_PROBE, stock_kexec.UPLOAD, stock_kexec.KEXEC]:
            subprocess.run(["bash", "-n"], input=script, text=True, check=True, timeout=5)


if __name__ == "__main__":
    unittest.main()
