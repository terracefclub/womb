"""Unit tests for lease_host_steps.py: inputs, selection, pinned SSH, redaction, G1 and clean-up, evidence."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lease_host_steps as steps  # noqa: E402

HOST_KEY = "A" * 68
SFTP_KEY = "B" * 68
FAKE_PRIVATE = ("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAAprivatekeybodyline0123456789\n"
                "-----END OPENSSH " + "PRIVATE KEY-----\n")
STORED_VALUE = "0123456789abcdef" * 4
STORE_TREE = {"backup": {"primary": {"repository-password": STORED_VALUE}},
              "website": {"payload-secret": "f" * 64}, "tls": {"certificate": "PUBLIC CERTIFICATE BODY LINE 1234"},
              "short": "abc123", "sops": {"mac": "metadata, not a secret value of the store"}}
PATCHES = []


def setUpModule():
    # The tests have no Nix store: the seven variant paths "exist".
    patch = mock.patch.object(steps, "store_path_exists", return_value=True)
    patch.start()
    PATCHES.append(patch)


def tearDownModule():
    for patch in PATCHES:
        patch.stop()


def ssh_keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)


def nix_secret(name):
    return f"{name}:{'C' * 86}==\n"


class RunDirectory:
    """A run directory with every input `run` checks, and stand-in tools."""

    def __init__(self, root):
        self.root = Path(root)
        self.run = self.root / "run"
        self.run.mkdir(mode=0o700)
        for name in (steps.BOOTSTRAP_KEY, steps.OPERATOR_KEY):
            ssh_keygen(self.run / name)
        self.private(steps.ESCROW, FAKE_PRIVATE)
        self.private(steps.DEPLOY_KEY, nix_secret("terrace-lease-t-deploy-1"))
        self.private(steps.SPARE_KEY, nix_secret("terrace-lease-t-spare-1"))
        (self.run / steps.RUN_CA).write_text("-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n")
        hosts = self.run / steps.STORE / "hosts" / steps.HOST
        hosts.mkdir(parents=True)
        os.chmod(self.run / steps.STORE, 0o700)
        (hosts / "ssh_host_ed25519_key.pub").write_text(f"ssh-ed25519 {HOST_KEY} root@app\n")
        (self.run / steps.SFTP).mkdir(mode=0o700)
        (self.run / steps.INPUTS_JSON).write_text(json.dumps({"backupPort": 22433, "sftpHostKey": f"ssh-ed25519 {SFTP_KEY}", "escrowRecipient": "age1" + "q" * 58,
                                                               "wombRevision": "0" * 40}))
        (self.run / steps.KNOWN_HOSTS).write_text(f"[127.0.0.1]:2222 ssh-ed25519 {HOST_KEY}\n")
        self.variants = self.root / "variants.json"
        self.variants.write_text(json.dumps({name: f"/nix/store/{'abcdfghij'[i] * 32}-nixos-system-app-{name}"
                                             for i, name in enumerate(steps.VARIANTS)}))
        self.tool = self.root / "tool"
        self.tool.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.tool, 0o755)
        # sops decrypt of the whole store: what the redactor loads before the first step.
        self.sops = self.root / "sops"
        self.sops.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(STORE_TREE)}\nEOF\n")
        os.chmod(self.sops, 0o755)
        self.evidence = self.root / "evidence"

    def private(self, name, text):
        path = self.run / name
        path.write_text(text)
        os.chmod(path, 0o600)

    def argv(self, *extra):
        return ["run", str(self.run), "--address", "127.0.0.1", "--port", "2222", "--evidence", str(self.evidence),
                "--variants", str(self.variants), "--rclone", str(self.tool), "--restic", str(self.tool),
                "--sops", str(self.sops), "--terrace-secrets", str(self.tool), "--curl", str(self.tool),
                "--ssh", str(self.tool), "--nix", str(self.tool), *extra]


def quiet(function, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = function(*args)
    return code, out.getvalue(), err.getvalue()


class Listing(unittest.TestCase):
    def test_list_names_every_step_with_guard_expected_and_evidence(self):
        code, out, _ = quiet(steps.main, ["list"])
        self.assertEqual(code, 0)
        for step in steps.STEPS:
            self.assertIn(f"{step.id:4} {step.title}", out)
        self.assertEqual(out.count("guard:"), len(steps.STEPS))
        self.assertEqual(out.count("evidence:"), len(steps.STEPS))

    def test_the_steps_are_the_run_sheet_order(self):
        self.assertEqual(steps.STEP_IDS, ("2.1", "3.1", "3.2", "3.3", "3.4", "3.5", "4.1", "5.1", "5.2", "5.3", "5.4",
                                          "5.5", "6"))
        self.assertEqual(set(steps.STEP_FUNCTIONS), set(steps.STEP_IDS))

    def test_run_certificates_outlast_the_hosts_expiry_check(self):
        # The leaf must keep more than the check's 14 days for a run of hours, with a week of margin.
        self.assertGreaterEqual(steps.LEAF_DAYS, steps.TLS_CHECK_DAYS + 7)
        self.assertEqual(steps.TLS_CHECK_DAYS, 14)

    def test_refusal_probes_are_paced_past_one_penalty(self):
        # OpenSSH 10.6 defaults: authfail and invaliduser 5 s each, enforced from 15 s accrued.
        self.assertGreater(steps.PROBE_PAUSE, 5)

    def test_selection(self):
        self.assertEqual([s.id for s in steps.select_steps()], list(steps.STEP_IDS))
        self.assertEqual([s.id for s in steps.select_steps(start="5.3")], ["5.3", "5.4", "5.5", "6"])
        self.assertEqual([s.id for s in steps.select_steps(end="3.1")], ["2.1", "3.1"])
        self.assertEqual([s.id for s in steps.select_steps(start="3.2", end="3.3")], ["3.2", "3.3"])
        self.assertEqual([s.id for s in steps.select_steps(only="6,2.1")], ["2.1", "6"])
        for kwargs in ({"only": "9.9"}, {"only": "2.1,2.1"}, {"only": ","}, {"start": "5.1", "end": "3.1"},
                       {"start": "7"}):
            with self.assertRaises(steps.Refused):
                steps.select_steps(**kwargs)


class Inputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def refused(self, *extra):
        existed = (self.dir.run / steps.WORK).exists()
        code, _, err = quiet(steps.main, self.dir.argv(*extra))
        self.assertEqual(code, steps.EXIT_INPUTS, err)
        self.assertFalse(self.dir.evidence.exists(), "nothing written for refused inputs")
        self.assertEqual((self.dir.run / steps.WORK).exists(), existed)
        return err

    def test_dry_run_accepts_and_lists_without_contacting(self):
        with mock.patch.object(steps.Driver, "execute", side_effect=AssertionError("no command")):
            code, out, _ = quiet(steps.main, self.dir.argv("--dry-run", "--from", "5.1"))
        self.assertEqual(code, 0)
        self.assertIn("would run against [127.0.0.1]:2222", out)
        self.assertIn("5.1  V1", out)
        self.assertNotIn("4.1  reboot", out)
        self.assertFalse(self.dir.evidence.exists())

    def test_wrong_pin_refused(self):
        (self.dir.run / steps.KNOWN_HOSTS).write_text(f"127.0.0.1 ssh-ed25519 {HOST_KEY}\n")
        self.assertIn("does not pin", self.refused())

    def test_pin_for_another_key_refused(self):
        (self.dir.run / steps.KNOWN_HOSTS).write_text(f"[127.0.0.1]:2222 ssh-ed25519 {'Z' * 68}\n")
        self.refused()

    def test_open_run_directory_refused(self):
        os.chmod(self.dir.run, 0o755)
        self.assertIn("private", self.refused())

    def test_readable_key_refused(self):
        os.chmod(self.dir.run / steps.DEPLOY_KEY, 0o644)
        self.refused()

    def test_same_deploy_keys_refused(self):
        (self.dir.run / steps.SPARE_KEY).write_text(nix_secret("terrace-lease-t-deploy-1"))
        self.assertIn("differ", self.refused())

    def test_variants_must_be_the_seven_systems(self):
        data = json.loads(self.dir.variants.read_text())
        del data["v5"]
        self.dir.variants.write_text(json.dumps(data))
        self.assertIn("variants", self.refused())

    def test_leftover_scratch_refused(self):
        (self.dir.run / steps.WORK).mkdir(mode=0o700)
        self.assertIn("did not clean up", self.refused())

    def test_existing_evidence_refused(self):
        self.dir.evidence.mkdir()
        code, _, err = quiet(steps.main, self.dir.argv())
        self.assertEqual(code, steps.EXIT_INPUTS)
        self.assertIn("evidence directory", err)

    def test_address_must_be_ipv4(self):
        argv = self.dir.argv()
        argv[argv.index("127.0.0.1")] = "app.example"
        code, _, _ = quiet(steps.main, argv)
        self.assertEqual(code, steps.EXIT_INPUTS)

    def test_tool_must_be_executable(self):
        os.chmod(self.dir.tool, 0o644)
        self.refused()

    def test_steps_and_to_refused(self):
        code, _, _ = quiet(steps.main, self.dir.argv("--steps", "2.1", "--to", "3.1"))
        self.assertEqual(code, steps.EXIT_INPUTS)


class Pin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        (self.dir.run / steps.KNOWN_HOSTS).unlink()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_port_22_uses_the_bare_address(self):
        code, _, _ = quiet(steps.main, ["pin", str(self.dir.run), "--address", "203.0.113.10"])
        self.assertEqual(code, 0)
        self.assertEqual((self.dir.run / steps.KNOWN_HOSTS).read_text(), f"203.0.113.10 ssh-ed25519 {HOST_KEY}\n")
        self.assertEqual(os.stat(self.dir.run / steps.KNOWN_HOSTS).st_mode & 0o777, 0o600)

    def test_other_port_uses_brackets_and_never_replaces(self):
        argv = ["pin", str(self.dir.run), "--address", "127.0.0.1", "--port", "2222"]
        self.assertEqual(quiet(steps.main, argv)[0], 0)
        self.assertEqual((self.dir.run / steps.KNOWN_HOSTS).read_text(), f"[127.0.0.1]:2222 ssh-ed25519 {HOST_KEY}\n")
        self.assertEqual(quiet(steps.main, argv)[0], steps.EXIT_INPUTS)


def driver(directory, evidence=None):
    run, variants, inputs = steps.check_run(directory.run, "127.0.0.1", 2222, directory.variants)
    return steps.Driver(run=run, address="127.0.0.1", port=2222,
                        evidence=evidence or steps.Evidence(directory.evidence),
                        tools=steps.Tools(rclone="r", restic="r", sops="s", terrace_secrets="t"), variants=variants,
                        inputs=inputs)


class PinnedSsh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        self.driver = driver(self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_every_ssh_is_pinned_and_batch(self):
        argv = self.driver.ssh_argv("true")
        joined = " ".join(argv)
        for option in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null", "BatchMode=yes",
                       "IdentitiesOnly=yes", "HostKeyAlgorithms=ssh-ed25519", "UpdateHostKeys=no",
                       f"UserKnownHostsFile={self.dir.run.resolve() / steps.KNOWN_HOSTS}"):
            self.assertIn(option, joined)
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-i") + 1], str(self.dir.run.resolve() / steps.OPERATOR_KEY))
        self.assertEqual(argv[-2:], ["operator@127.0.0.1", "true"])
        self.assertNotIn("accept-new", joined)
        self.assertNotIn("StrictHostKeyChecking=no", joined)

    def test_agent_never_passed(self):
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent", "SSH_AGENT_PID": "1"}):
            env = self.driver.env()
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("SSH_AGENT_PID", env)

    def test_user_manager_reachable_from_the_lease_drivers_bare_environment(self):
        bare = {"PATH": "/bin", "HOME": "/tmp", "USER": "u", "LOGNAME": "u", "LANG": "C.UTF-8", "TZ": "UTC"}
        runtime = f"/run/user/{os.geteuid()}"
        with mock.patch.dict(os.environ, bare, clear=True), \
                mock.patch.object(steps.os.path, "isdir", side_effect=lambda path: path == runtime):
            env = self.driver.env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], runtime)
        with mock.patch.dict(os.environ, dict(bare, XDG_RUNTIME_DIR="/elsewhere"), clear=True):
            self.assertEqual(self.driver.env()["XDG_RUNTIME_DIR"], "/elsewhere")

    def test_root_scripts_go_through_sudo(self):
        with mock.patch.object(self.driver, "execute", return_value=(0, b"", b"", 0.1)) as execute:
            self.driver.root("systemctl reboot")
        remote = execute.call_args[0][0][-1]
        self.assertTrue(remote.startswith("sudo -n bash -c "), remote)

    def test_non_zero_exit_fails_the_step(self):
        self.driver.evidence.open_step(steps.STEPS[0])
        with mock.patch.object(self.driver, "execute", return_value=(1, b"", b"no", 0.1)):
            with self.assertRaises(steps.Failed):
                self.driver.ssh("false")
            self.assertEqual(self.driver.ssh("false", ok=None).code, 1)

    def test_transcript_records_command_and_exit(self):
        self.driver.evidence.open_step(steps.STEPS[0])
        with mock.patch.object(self.driver, "execute", return_value=(0, b"hello\n", b"", 0.25)):
            self.driver.ssh("echo hello", shown="echo hello")
        self.driver.evidence.close_step()
        text = (self.dir.evidence / "transcripts" / "2.1.log").read_text()
        self.assertIn("$ ssh operator@host echo hello", text)
        self.assertIn("# exit 0", text)
        self.assertIn("hello", text)


class Redaction(unittest.TestCase):
    def test_private_material_refused(self):
        redactor = steps.Redactor()
        redactor.add_bytes(b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----\nAAAAsecretbodyline0123456789\n-----END OPENSSH PRIVATE KEY-----\n")
        redactor.add_bytes(nix_secret("k").encode())
        redactor.add_bytes(b"0123456789abcdef0123456789abcdef\n")
        for data in (b"x AAAAsecretbodyline0123456789 y", b"C" * 86 + b"==", b"-----BEGIN " + b"PRIVATE KEY-----",
                     b"AGE-SECRET-KEY-1QQQ", b"pw 0123456789abcdef0123456789abcdef"):
            with self.assertRaises(steps.Leak):
                redactor.check(data)
        self.assertEqual(redactor.check(b"ssh-ed25519 AAAA public"), b"ssh-ed25519 AAAA public")

    def test_leaking_command_output_is_never_written(self):
        tmp = tempfile.mkdtemp()
        try:
            directory = RunDirectory(tmp)
            d = driver(directory)
            d.redactor.add_file(directory.run / steps.ESCROW)
            d.evidence.open_step(steps.STEPS[0])
            with mock.patch.object(d, "execute", return_value=(0, b"AAAAprivatekeybodyline0123456789", b"", 0.1)):
                with self.assertRaises(steps.Leak):
                    d.ssh("cat key")
            d.evidence.close_step()
            self.assertNotIn("AAAAprivatekeybodyline", (directory.evidence / "transcripts" / "2.1.log").read_text())
        finally:
            shutil.rmtree(tmp)


class SecretReads(unittest.TestCase):
    """dev run 6 (2026-11-27): step 6 read the repository password a second time for its off-host check, and the
    guard refused the read because step 3.4 had added that value to the known secrets. The read itself is the
    driver's own (kept in memory, never written); every other output carrying the value is still refused, and a
    refusal names the command and the rule, never the value."""

    PASSWORD = b"0123456789abcdef" * 4

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        self.driver = driver(self.dir)
        self.driver.redactor.add_bytes(self.PASSWORD, "the backup repository password")
        self.driver.evidence.open_step(steps.STEPS[-1])

    def tearDown(self):
        self.driver.evidence.close_step()
        shutil.rmtree(self.tmp)

    def transcript(self):
        return (self.dir.evidence / "transcripts" / "6.log").read_text()

    def test_reading_a_known_secret_again_is_not_refused_and_never_written(self):
        with mock.patch.object(self.driver, "pipe", return_value=(0, self.PASSWORD + b"\n", b"", 0.1)):
            value = self.driver.read_secret(["sops", "decrypt"], 60, shown="sops decrypt --extract backup/primary/repository-password")
            again = self.driver.read_secret(["sops", "decrypt"], 60, shown="sops decrypt --extract backup/primary/repository-password")
        self.assertEqual(value.strip().encode(), self.PASSWORD)
        self.assertEqual(again, value)
        self.assertNotIn(self.PASSWORD.decode(), self.transcript())
        self.assertIn("standard output is a secret: not kept", self.transcript())

    def test_any_other_output_with_the_value_is_refused_naming_command_and_rule(self):
        with mock.patch.object(self.driver, "execute", return_value=(0, b"pw " + self.PASSWORD, b"", 0.1)):
            with self.assertRaises(steps.Leak) as caught:
                self.driver.root("journalctl -p err -b", shown="journalctl -p err -b")
        message = str(caught.exception)
        self.assertIn("journalctl -p err -b", message)
        self.assertIn("the backup repository password", message)
        self.assertNotIn(self.PASSWORD.decode(), message + self.transcript())
        self.assertIn("REFUSED: its stdout carried the backup repository password", self.transcript())

    def test_secret_reads_still_check_standard_error(self):
        with mock.patch.object(self.driver, "pipe", return_value=(0, b"x", b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----", 0.1)):
            with self.assertRaises(steps.Leak) as caught:
                self.driver.read_secret(["sops"], 60, shown="sops decrypt")
        self.assertIn("a PEM or OpenSSH private key marker", str(caught.exception))

    def test_rules_name_their_source(self):
        redactor = steps.Redactor()
        redactor.add_file(self.dir.run / steps.ESCROW)
        self.assertEqual(redactor.rule(b"AAAAprivatekeybodyline0123456789"), f"a line of {steps.ESCROW}")
        self.assertEqual(redactor.rule(b"AGE-SECRET-KEY-1X"), "an age secret key marker")
        self.assertIsNone(redactor.rule(b"ssh-ed25519 public"))

    def test_the_receipt_names_command_and_rule(self):
        def leaking(d):
            d.root("cat /var/log/x", shown="cat /var/log/x")
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"6": leaking}), \
                mock.patch.object(steps.Driver, "execute", return_value=(0, self.PASSWORD, b"", 0.1)), \
                mock.patch.object(steps.Driver, "cleanup", return_value=True), \
                mock.patch.object(steps.Redactor, "add_file", lambda self, path: self.add_bytes(
                    SecretReads.PASSWORD, "the backup repository password") if path.name == steps.ESCROW else None):
            shutil.rmtree(self.dir.evidence)
            code, _, _ = quiet(steps.main, self.dir.argv("--steps", "6"))
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        receipt = (self.dir.evidence / "receipts" / "6.json").read_text()
        self.assertIn("cat /var/log/x", receipt)
        self.assertIn("the backup repository password", receipt)
        self.assertNotIn(self.PASSWORD.decode(), receipt)


class HostFacts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        self.driver = driver(self.dir)
        self.driver.evidence.open_step(steps.STEPS[7])

    def tearDown(self):
        self.driver.evidence.close_step()
        shutil.rmtree(self.tmp)

    def answer(self, text):
        return mock.patch.object(self.driver, "execute", return_value=(0, text.encode(), b"", 0.1))

    @staticmethod
    def timers(backup, check="", sweep=""):
        return (f"Id=terrace-backup.timer\nNextElapseUSecRealtime={backup}\n\n"
                f"Id=terrace-backup-check.timer\nNextElapseUSecRealtime={check}\n\n"
                f"Id=terrace-cms-sweep.timer\nNextElapseUSecRealtime={sweep}\n")

    def test_timer_guard(self):
        soon = int(time.time()) + 300
        later = int(time.time()) + 3600
        with self.answer(self.timers(f"@{later}")):
            self.driver.timers_clear()
        for text in (self.timers(f"@{soon}"), self.timers("Sat 2026-11-28 03:22:11 UTC"),
                     # S1/N1: an empty or partial answer never passes the absence check.
                     "", f"Id=terrace-backup.timer\nNextElapseUSecRealtime=@{later}\n"):
            with self.subTest(text=text[:40]), self.answer(text):
                with self.assertRaises(steps.Failed):
                    self.driver.timers_clear()
        with mock.patch.object(self.driver, "execute", return_value=(255, b"", b"", 0.1)):
            with self.assertRaises(steps.Failed):
                self.driver.timers_clear()

    def test_unit_states(self):
        with self.answer("Id=a.service\nActiveState=active\nResult=success\nConditionResult=yes\n\n"
                         "Id=b.timer\nActiveState=inactive\nResult=success\nConditionResult=no\n"):
            states = self.driver.unit_states(["a.service", "b.timer"])
        self.assertEqual(states["a.service"]["ActiveState"], "active")
        self.assertEqual(states["b.timer"]["ConditionResult"], "no")

    def test_generation_of_the_newest_link(self):
        target = self.driver.variants["v3"]
        with self.answer(f"/nix/var/nix/profiles/system-5-link {target}\n/nix/var/nix/profiles/system-6-link /x\n"
                         f"/nix/var/nix/profiles/system-8-link {target}\n"):
            self.assertEqual(self.driver.generation_of(target), 8)

    def test_settled_allows_only_the_skipped_cms_before_migration(self):
        wants = "terrace-cms.service postgresql.service terrace-backup.timer"
        show = ("Id=terrace-cms.service\nActiveState=inactive\nResult=success\nConditionResult=no\n\n"
                "Id=postgresql.service\nActiveState=active\nResult=success\nConditionResult=yes\n\n"
                "Id=terrace-backup.timer\nActiveState=active\nResult=success\nConditionResult=yes\n")
        outputs = iter([wants, show, ""] * 2)
        with mock.patch.object(self.driver, "execute", side_effect=lambda *a, **k: (0, next(outputs).encode(), b"", 0.1)):
            self.driver.settled(cms_running=False)
            with self.assertRaises(steps.Failed):
                self.driver.settled(cms_running=True)


class StopAtFirstFailure(unittest.TestCase):
    """G1: the first failed step stops the run; clean-up runs; exit codes; evidence and SHA256SUMS."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def functions(self, failing=None, error=steps.Failed("guard")):
        def make(step_id):
            def function(_driver):
                self.calls.append(step_id)
                if step_id == failing:
                    raise error
                return {"ok": step_id}
            return function
        return {step_id: make(step_id) for step_id in steps.STEP_IDS}

    def run_main(self, functions, cleanup=True, extra=()):
        with mock.patch.dict(steps.STEP_FUNCTIONS, functions), \
                mock.patch.object(steps.Driver, "cleanup", return_value=cleanup) as clean:
            code, out, _ = quiet(steps.main, self.dir.argv(*extra))
        return code, out, clean

    def test_pass(self):
        code, out, clean = self.run_main(self.functions())
        self.assertEqual(code, steps.EXIT_PASS)
        self.assertEqual(self.calls, list(steps.STEP_IDS))
        clean.assert_called_once()
        evidence = self.dir.evidence
        self.assertTrue((evidence / "SHA256SUMS").is_file())
        listed = (evidence / "SHA256SUMS").read_text()
        for name in ("steps.log", "ledger.json", "summary.json", "receipts/5.5.json", "transcripts/cleanup.log"):
            self.assertIn(f"  {name}\n", listed)
        self.assertEqual(json.loads((evidence / "summary.json").read_text())["result"], "pass")
        receipt = json.loads((evidence / "receipts" / "3.4.json").read_text())
        self.assertEqual((receipt["status"], receipt["observations"]), ("passed", {"ok": "3.4"}))
        log = (evidence / "steps.log").read_text().splitlines()
        self.assertEqual(sum(" exit 0 " in line for line in log), len(steps.STEP_IDS))
        self.assertTrue(log[-1].endswith("cleanup exit 0"))
        self.assertFalse((self.dir.run / steps.WORK).exists() and any((self.dir.run / steps.WORK).iterdir()))

    def test_first_failure_stops_without_retry(self):
        code, _, clean = self.run_main(self.functions(failing="3.3"))
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        self.assertEqual(self.calls, ["2.1", "3.1", "3.2", "3.3"])
        clean.assert_called_once()
        summary = json.loads((self.dir.evidence / "summary.json").read_text())
        self.assertEqual((summary["failedStep"], summary["exit"]), ("3.3", 1))
        self.assertFalse((self.dir.evidence / "receipts" / "3.4.json").exists())
        self.assertEqual(json.loads((self.dir.evidence / "receipts" / "3.3.json").read_text())["error"], "guard")

    def test_unexpected_error_is_a_failed_step_with_its_type_only(self):
        code, _, _ = self.run_main(self.functions(failing="2.1", error=AttributeError("secret text")))
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        receipt = (self.dir.evidence / "receipts" / "2.1.json").read_text()
        self.assertIn("AttributeError in step 2.1", receipt)
        self.assertNotIn("secret text", receipt)

    def test_cleanup_not_verified(self):
        code, _, _ = self.run_main(self.functions(), cleanup=False)
        self.assertEqual(code, steps.EXIT_CLEANUP)

    def test_interrupted(self):
        code, _, clean = self.run_main(self.functions(failing="4.1", error=steps.Interrupted()))
        self.assertEqual(code, steps.EXIT_INTERRUPTED)
        clean.assert_called_once()

    def test_selection_runs_only_the_selected(self):
        code, _, _ = self.run_main(self.functions(), extra=("--steps", "5.3,6"))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["5.3", "6"])

    def test_observations_with_a_secret_fail_the_step(self):
        functions = self.functions()
        functions["2.1"] = lambda _d: {"oops": "AAAAprivatekeybodyline0123456789"}
        code, _, _ = self.run_main(functions)
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        self.assertNotIn("AAAAprivatekeybodyline", (self.dir.evidence / "receipts" / "2.1.json").read_text())


class Cleanup(unittest.TestCase):
    def test_stops_only_its_own_processes_and_removes_its_scratch(self):
        tmp = tempfile.mkdtemp()
        try:
            directory = RunDirectory(tmp)
            d = driver(directory)
            d.work.mkdir(mode=0o700)
            (d.work / "repository-password").write_text("x" * 64)
            d.evidence.open_step(steps.STEPS[0])
            process = d.start_background("second-session", [sys.executable, "-c", "import time; time.sleep(60)"])
            self.assertTrue(d.alive("second-session"))
            self.assertTrue(d.cleanup())
            self.assertIsNotNone(process.poll())
            self.assertFalse(d.work.exists())
            self.assertEqual(d.processes, {})
            for name in (steps.OPERATOR_KEY, steps.DEPLOY_KEY, steps.STORE):
                self.assertTrue((directory.run / name).exists(), "the caller's material stays")
        finally:
            shutil.rmtree(tmp)

    def test_sftp_stopped_only_when_started_here(self):
        tmp = tempfile.mkdtemp()
        try:
            d = driver(RunDirectory(tmp))
            d.evidence.open_step(steps.STEPS[0])
            with mock.patch.object(d, "execute", return_value=(0, b"", b"", 0.1)) as execute:
                self.assertTrue(d.cleanup())
                execute.assert_not_called()
            d.sftp_attempted = True
            fake = Fake([UNITS_RULE, (r"rehearsal_sftp.py stop", (0, "", "")), (r"list-units", (0, "", ""))])
            with mock.patch.object(d, "execute", side_effect=fake):
                self.assertTrue(d.cleanup())
            self.assertTrue(fake.ran(r"rehearsal_sftp.py stop"))
            self.assertIn(OURS, fake.ran(r"list-units")[-1])
        finally:
            shutil.rmtree(tmp)


def stub_prepare(tmp):
    """`prepare` with stand-in tools in TMP; returns (run, code, out, err)."""
    root = Path(tmp)
    run = root / "run"
    run.mkdir(mode=0o700)
    archive = root / "a.tar.gz"
    archive.write_text("x")
    stub = root / "terrace-secrets"
    stub.write_text(f"""#!{sys.executable}
import os, sys
from pathlib import Path
store = Path(os.environ["TERRACE_SECRETS_STORE"])
store.mkdir(mode=0o700, exist_ok=True)
args = sys.argv[1:]
if args[:2] == ["escrow", "create"]:
    Path(args[2]).write_text("AGE-SECRET-KEY-1STUB\\n"); os.chmod(args[2], 0o600)
    (store / "escrow.recipient").write_text("age1" + "q" * 58 + "\\n")
elif args[:2] == ["host", "create"]:
    os.makedirs(args[args.index("--stage") + 1], mode=0o700)
elif args[0] == "public":
    print("ssh-ed25519 " + "D" * 68 + " client")
elif args[0] == "set":
    sys.stdin.read()
""")
    os.chmod(stub, 0o755)
    nix = root / "nix"
    nix.write_text(f"""#!{sys.executable}
import sys
if "generate-secret" in sys.argv:
    print(sys.argv[-1] + ":" + "E" * 86 + "==", end="")
else:
    sys.stdout.write(sys.stdin.read().split(":")[0] + ":" + "F" * 43 + "=")
""")
    os.chmod(nix, 0o755)
    argv = ["prepare", str(run), "--terrace-secrets", str(stub), "--openssl", shutil.which("openssl"),
            "--ssh-keygen", shutil.which("ssh-keygen"), "--nix", str(nix), "--web-archive", str(archive),
            "--cms-archive", str(archive), "--previous-web-archive", str(archive),
            "--previous-cms-archive", str(archive), "--tfc-source", tmp, "--womb-revision", "1" * 40,
            "--key-tag", "t1"]
    # The SFTP helper needs the pinned ssh-keygen; stand in for it.
    real = subprocess.run

    def run_stub(command, *args, **kwargs):
        if str(steps.SFTP_TOOL) in command:
            directory = Path(command[command.index("--directory") + 1])
            (directory / "sftp-host-key.pub").write_text(f"ssh-ed25519 {SFTP_KEY}\n")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real(command, *args, **kwargs)

    with mock.patch.object(steps.subprocess, "run", side_effect=run_stub):
        code, out, err = quiet(steps.main, argv)
    return run, code, out, err


class Prepare(unittest.TestCase):
    def test_refuses_existing_material_and_missing_archives(self):
        tmp = tempfile.mkdtemp()
        try:
            run = Path(tmp) / "run"
            run.mkdir(mode=0o700)
            archive = Path(tmp) / "a.tar.gz"
            archive.write_text("x")
            base = ["prepare", str(run), "--terrace-secrets", "t", "--openssl", "o", "--ssh-keygen", "k",
                    "--web-archive", str(archive), "--cms-archive", str(archive), "--previous-web-archive", str(archive),
                    "--previous-cms-archive", str(Path(tmp) / "missing"), "--tfc-source", tmp,
                    "--womb-revision", "0" * 40]
            self.assertEqual(quiet(steps.main, base)[0], steps.EXIT_INPUTS)
            base[base.index(str(Path(tmp) / "missing"))] = str(archive)
            (run / steps.ESCROW).write_text("x")
            code, _, err = quiet(steps.main, base)
            self.assertEqual(code, steps.EXIT_INPUTS)
            self.assertIn("already exists", err)
        finally:
            shutil.rmtree(tmp)

    @unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("openssl"), "needs ssh-keygen and openssl")
    def test_material_with_stub_tools(self):
        """Every file created new and private; nothing secret printed; the scratch TLS files are gone."""
        tmp = tempfile.mkdtemp()
        try:
            run, code, out, err = stub_prepare(tmp)
            self.assertEqual(code, 0, err)
            self.assertNotIn("PRIVATE", out + err)
            for name in (steps.OPERATOR_KEY, steps.DEPLOY_KEY, steps.SPARE_KEY):
                self.assertEqual((run / name).stat().st_mode & 0o777, 0o600)
            inputs = json.loads((run / steps.INPUTS_JSON).read_text())
            self.assertEqual(inputs["backupPort"], 22433)
            self.assertEqual(inputs["deployKeys"][0].split(":")[0], "terrace-lease-t1-deploy-1")
            self.assertEqual(inputs["deployKeys"][1].split(":")[0], "terrace-lease-t1-spare-1")
            self.assertEqual(inputs["sftpHostKey"], f"ssh-ed25519 {SFTP_KEY}")
            nix_text = (run / steps.INPUTS_NIX).read_text()
            self.assertIn(f"runCa = {run.resolve() / steps.RUN_CA};", nix_text)
            self.assertIn("backupPort = 22433;", nix_text)
            self.assertFalse(any(path.name.startswith("tls-") for path in run.iterdir()), "TLS scratch removed")
            self.assertNotIn("PRIVATE", (run / steps.RUN_CA).read_text())
            # The run CA (and the leaf, checked inside prepare) clears the host's 14-day expiry check by a margin.
            self.assertEqual(subprocess.run([shutil.which("openssl"), "x509", "-noout", "-checkend", str(21 * 86400),
                                             "-in", str(run / steps.RUN_CA)], capture_output=True).returncode, 0)
        finally:
            shutil.rmtree(tmp)


# ---- The steps' own safety logic, driven with a scripted host ------------------------------------------------------

import re  # noqa: E402

OURS = "terrace-lease-sftp-0123456789abcdef.service"
OURS_WATCH = "terrace-lease-sftp-0123456789abcdef-watch.service"
UNITS_RULE = (r"rehearsal_sftp.py units", (0, json.dumps({"unit": OURS, "watcher": OURS_WATCH}), ""))
BOOT_A, BOOT_B = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"


class Fake:
    """A scripted `execute`: the first rule whose pattern matches the joined command answers (code, stdout, stderr);
    an unexpected command fails the test's step."""

    def __init__(self, rules):
        self.rules = list(rules)
        self.calls = []

    def __call__(self, argv, timeout, *, stdin=None, env=None):
        text = " ".join(argv)
        self.calls.append((list(argv), env))
        for pattern, answer in self.rules:
            if re.search(pattern, text):
                code, out, err = answer(argv) if callable(answer) else answer
                return code, out.encode(), err.encode(), 0.1
        raise AssertionError("unexpected command: " + text[-160:])

    def ran(self, pattern):
        return [argv for argv, _ in self.calls if re.search(pattern, " ".join(argv))]


class StepHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = RunDirectory(self.tmp)
        self.d = driver(self.dir)
        self.d.work.mkdir(mode=0o700)
        self.d.evidence.open_step(steps.STEPS[0])
        self.sleeps = []
        for patch in (mock.patch.object(steps.time, "sleep", side_effect=self.sleeps.append),):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.d.evidence.close_step()
        shutil.rmtree(self.tmp)

    def host(self, rules):
        fake = Fake(rules)
        patch = mock.patch.object(self.d, "execute", side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)
        return fake

    def fails(self, function, *args, text=None):
        with self.assertRaises(steps.Failed) as caught:
            function(*args)
        if text:
            self.assertIn(text, str(caught.exception))
        return caught.exception


def first_boot_rules(d, **overrides):
    v0 = d.variants["v0"]
    rules = {
        "wrong": (r"wrong-known-hosts", (255, "", "Host key verification failed.\n")),
        "password": (r"PubkeyAuthentication=no", (255, "", "operator@127.0.0.1: Permission denied (publickey).\n")),
        "bootstrap-root": (r"/operator-ed25519 .* root@127\.0\.0\.1 true$",
                           (255, "", "root@127.0.0.1: Permission denied (publickey).\n")),
        "bootstrap": (r"/operator-ed25519 .* operator@127\.0\.0\.1 true$",
                      (255, "", "operator@127.0.0.1: Permission denied (publickey).\n")),
        "root": (r"root@127\.0\.0\.1 true$", (255, "", "root@127.0.0.1: Permission denied (publickey).\n")),
        "operator": (r"operator@127\.0\.0\.1 true$", (0, "", "")),
        "boot": (r"boot_id", (0, BOOT_A + "\n", "")),
        "systems": (r"readlink -f /run/current-system", (0, f"{v0}\n{v0}\n{v0}\n", "")),
        "status": (r"terrace-production status",
                   (0, "not adopted: no writer starts at boot\ninactive\ndeploy path: on, 2 trusted deploy key(s)\n", "")),
        "wants": (r"terrace-production.target -p Wants", (0, "a.service b.timer\n", "")),
        "sops": (r"sops-install-secrets.service",
                 (0, "Id=sops-install-secrets.service\nActiveState=inactive\nResult=exit-code\nConditionResult=yes\n", "")),
        "units": (r"systemctl show -p Id -p ActiveState", (0, "\n\n".join(
            f"Id={u}\nActiveState=inactive\nResult=success\nConditionResult=yes" for u in
            ("terrace-production.target", "a.service", "b.timer")) + "\n", "")),
        "listen": (r"ss -Htln", (0, "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 [::]:22 [::]:*\n", "")),
        "efi": (r"test -d /sys/firmware/efi", (0, "", "")),
        "pub": (r"test -e /etc/ssh/ssh_host_ed25519_key.pub", (1, "", "")),
        "bootctl": (r"bootctl status", (0, "     Product: systemd-boot 258\n", "")),
        "ip": (r"ip -4 -j addr", (0, json.dumps([{"ifname": "enp0s3", "addr_info": [
            {"family": "inet", "local": "10.0.2.15", "prefixlen": 24, "dynamic": True}]}]), "")),
        "df": (r"df -B1", (0, "Mounted on 1B-blocks\n/ 1\n/boot 1\n", "")),
        "closure": (r"path-info -S /run/current-system", (0, "/nix/store/x 2763041312\n", "")),
    }
    rules.update(overrides)
    return list(rules.values())


class FirstBootWindow(StepHarness):
    def test_the_good_transcript_passes_with_paced_probes_and_the_login_proven_twice(self):
        fake = self.host(first_boot_rules(self.d))
        observed = steps.step_2_1(self.d)
        self.assertEqual(self.sleeps, [steps.PROBE_PAUSE] * 5)
        logins = [argv for argv in fake.ran(r"/host-operator-ed25519 .* operator@127\.0\.0\.1 true$")
                  if "PubkeyAuthentication=no" not in argv and not any("wrong-known-hosts" in a for a in argv)]
        self.assertEqual(len(logins), 2)
        self.assertEqual(observed["listeningPorts"], ["22"])
        self.assertIs(observed["hostKeyPubPresent"], False)
        self.assertEqual(observed["dhcp"], [("enp0s3", "10.0.2.15", 24)])
        self.assertFalse(os.path.lexists(self.d.work / "wrong-known-hosts"), "the wrong pin is removed")

    def test_each_refusal_must_be_a_refusal(self):
        cases = {
            "root": (r"root@127\.0\.0\.1 true$", (0, "", "")),
            # Only exit 255 is a refusal, whatever the message says.
            "root-exit-0": (r"root@127\.0\.0\.1 true$", (0, "", "root@127.0.0.1: Permission denied (publickey).\n")),
            "bootstrap": (r"/operator-ed25519 .* operator@127\.0\.0\.1 true$", (0, "", "")),
            "bootstrap-root": (r"/operator-ed25519 .* root@127\.0\.0\.1 true$", (0, "", "")),
            "wrong": (r"wrong-known-hosts", (0, "", "")),
            "password": (r"PubkeyAuthentication=no", (255, "", "Permission denied (publickey,password).\n")),
        }
        for name, rule in cases.items():
            with self.subTest(probe=name):
                self.setUp()
                self.host(first_boot_rules(self.d, **{name.replace("-exit-0", ""): rule}))
                self.fails(steps.step_2_1, self.d)
                self.assertFalse(os.path.lexists(self.d.work / "wrong-known-hosts"))

    def test_a_refusal_with_the_wrong_message_fails(self):
        self.host(first_boot_rules(self.d, root=(r"root@127\.0\.0\.1 true$", (255, "", "Connection closed by 127.0.0.1\n"))))
        self.fails(steps.step_2_1, self.d, text="seen: Connection closed by 127.0.0.1")

    def test_adopted_listening_or_secrets_installed_fail(self):
        for name, rule in (("status", (r"terrace-production status", (0, "adopted 2026-11-27T10:00:00Z\nactive\ndeploy path: on\n", ""))),
                           ("listen", (r"ss -Htln", (0, "LISTEN 0 128 0.0.0.0:22 *\nLISTEN 0 511 0.0.0.0:443 *\n", ""))),
                           ("sops", (r"sops-install-secrets.service", (0, "Id=sops-install-secrets.service\nActiveState=active\n"
                                                                       "Result=success\nConditionResult=yes\n", ""))),
                           ("ip", (r"ip -4 -j addr", (0, json.dumps([{"ifname": "e", "addr_info": [
                               {"family": "inet", "local": "10.0.2.15", "prefixlen": 24}]}]), "")))):
            with self.subTest(case=name):
                self.setUp()
                self.host(first_boot_rules(self.d, **{name: rule}))
                self.fails(steps.step_2_1, self.d)


class SftpAndForward(StepHarness):
    def test_an_interrupted_start_still_gets_a_stop_and_a_unit_check(self):
        def interrupted(_argv):
            raise steps.Interrupted()
        fake = self.host([(r"rehearsal_sftp.py start", interrupted), (r"rehearsal_sftp.py stop", (1, "", "")), UNITS_RULE,
                          (r"--user stop", (1, "", "")),
                          (r"list-units", (0, f"{OURS} loaded active running x\n", ""))])
        (self.d.run / steps.SFTP / "sftp-host-key.pub").write_text(f"ssh-ed25519 {SFTP_KEY}\n")
        with self.assertRaises(steps.Interrupted):
            steps.step_3_1(self.d)
        self.assertTrue(self.d.sftp_attempted)
        self.assertFalse(self.d.cleanup(), "a helper unit is still loaded: not verified")
        self.assertTrue(fake.ran(r"rehearsal_sftp.py stop"))
        self.assertIn("the SFTP endpoint not verified stopped", self.d.cleanup_failures)

    def test_no_receipt_and_no_unit_left_is_verified(self):
        self.d.sftp_attempted = True
        self.host([(r"rehearsal_sftp.py stop", (1, "", "")), UNITS_RULE, (r"list-units", (0, "", ""))])
        self.assertTrue(self.d.cleanup())

    def test_units_left_without_a_receipt_are_stopped_by_this_runs_names(self):
        self.d.sftp_attempted = True
        state = {"stopped": False}

        def stop(argv):
            state["stopped"] = True
            return 0, "", ""
        fake = self.host([(r"rehearsal_sftp.py stop", (1, "", "")), UNITS_RULE, (r"--user stop", stop),
                          (r"list-units", lambda argv: (0, "" if state["stopped"] else f"{OURS} loaded active running x\n{OURS_WATCH} loaded active running x\n", ""))])
        self.assertTrue(self.d.cleanup(), self.d.cleanup_failures)
        self.assertEqual(fake.ran(r"--user stop")[0][-2:], [OURS, OURS_WATCH])

    def test_another_runs_units_are_never_stopped(self):
        self.d.sftp_attempted = True
        other = "terrace-lease-sftp-fedcba9876543210.service"
        fake = self.host([(r"rehearsal_sftp.py stop", (1, "", "")), UNITS_RULE, (r"--user stop", (0, "", "")),
                          (r"list-units", (0, f"{other} loaded active running x\n", ""))])
        self.assertTrue(self.d.cleanup())
        self.assertEqual(fake.ran(r"--user stop"), [])
        self.assertNotIn(other, " ".join(fake.ran(r"list-units")[-1][:-2]))

    def test_a_failed_stop_or_listing_is_not_verified(self):
        for stop, listing in (((1, "", ""), (0, "", "")), ((0, "", ""), (1, "", ""))):
            with self.subTest(stop=stop[0], listing=listing[0]):
                self.setUp()
                self.d.sftp_attempted = True
                (self.d.run / steps.SFTP / "sftp-listener.json").write_text("{}")
                self.host([(r"rehearsal_sftp.py stop", stop), UNITS_RULE, (r"list-units", listing)])
                self.assertFalse(self.d.cleanup())

    def forward(self, option, listen):
        self.host([(r"rehearsal_sftp.py forward", (0, option, "")), (r"ss -Htln", (0, listen, "")),
                   (r"/dev/tcp/127\.0\.0\.1/22433", (0, "SSH-2.0-", ""))])
        for patch in (mock.patch.object(self.d, "start_background"), mock.patch.object(self.d, "alive", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_forward_binds_the_hosts_loopback_only(self):
        self.forward("-R 127.0.0.1:22433:127.0.0.1:40000\n", "LISTEN 0 128 127.0.0.1:22433 0.0.0.0:*\n")
        self.d.open_forward()
        self.setUp()
        self.forward("-R 127.0.0.1:22433:127.0.0.1:40000\n", "LISTEN 0 128 0.0.0.0:22433 0.0.0.0:*\n")
        self.fails(self.d.open_forward, text="loopback only")
        self.setUp()
        self.forward("-R 127.0.0.1:22433:127.0.0.1:40000 -o GatewayPorts=yes\n", "LISTEN 0 128 127.0.0.1:22433 *\n")
        self.fails(self.d.open_forward)

    def test_a_dead_forward_fails_unless_the_step_explains_it(self):
        self.host([(r"/dev/tcp/", (1, "", ""))])
        self.fails(self.d.require_forward, text="still answers")
        with mock.patch.object(self.d, "open_forward") as reopened:
            self.assertEqual(self.d.reopen_forward("the reboot"), {"reopened": True, "reason": "the reboot"})
        reopened.assert_called_once()


class OffHostCheck(StepHarness):
    def run_check(self, check_out="no errors were found\n", snapshots=1):
        fake = self.host([(r"check --read-data", (0, check_out, "")),
                          (r"snapshots --json", (0, json.dumps([{"hostname": "app"}] * snapshots), ""))])
        with mock.patch.object(self.d, "read_secret", return_value=STORED_VALUE + "\n"):
            result = steps.restic_offhost(self.d, 1)
        return fake, result

    def test_check_read_data_runs_and_the_password_file_is_gone(self):
        fake, result = self.run_check()
        self.assertTrue(fake.ran(r"--password-file .* check --read-data"))
        self.assertEqual(result, {"checkReadData": True, "snapshots": 1})
        self.assertFalse(os.path.lexists(self.d.work / "repository-password"))

    def test_errors_or_a_wrong_count_fail(self):
        self.fails(self.run_check, "Fatal: repository contains errors\n")
        self.setUp()
        self.fails(self.run_check, "no errors were found\n", 2)


class Reboot(StepHarness):
    def setUp(self):
        super().setUp()
        clock = iter(range(0, 10_000_000, 50))
        connection = mock.MagicMock()
        connection.__enter__.return_value.recv.return_value = b"SSH-2.0-OpenSSH"
        for patch in (mock.patch.object(steps.time, "monotonic", side_effect=lambda: next(clock)),
                      mock.patch.object(steps.socket, "create_connection", return_value=connection)):
            patch.start()
            self.addCleanup(patch.stop)

    def reboot_host(self, changes=True):
        state = {"rebooted": False}

        def boot(_argv):
            return 0, (BOOT_B if state["rebooted"] and changes else BOOT_A) + "\n", ""

        def reboot(_argv):
            state["rebooted"] = True
            return 255, "", ""
        return self.host([(r"systemctl show -p ActiveState", (0, "ActiveState=inactive\n" * 5, "")),
                          (r"list-jobs", (0, "", "")), (r"boot_id", boot), (r"systemctl reboot", reboot),
                          (r"is-system-running --wait", (0, "running\n", ""))])

    def test_back_with_a_new_boot_id(self):
        self.reboot_host()
        facts = self.d.reboot("t")
        self.assertEqual((facts["bootIdBefore"], facts["bootIdAfter"]), (BOOT_A, BOOT_B))

    def test_the_same_boot_id_is_never_taken_for_a_reboot(self):
        self.reboot_host(changes=False)
        self.fails(self.d.reboot, "t", text="came back within 900 s")


class Signatures(StepHarness):
    def test_names_are_compared_exactly(self):
        target = self.d.variants["v2"]
        names = {steps.DEPLOY_KEY: "x-deploy-1", steps.SPARE_KEY: "x-spare-1"}
        self.assertEqual(steps.signed_by(f"{target}  x-spare-1:AAA= cache.nixos.org-1:BBB=\n", target, names),
                         [steps.SPARE_KEY])
        self.assertEqual(steps.signed_by(f"{target}  ax-deploy-1:AAA= x-spare-1x:BBB=\n", target, names), [])
        self.assertEqual(steps.signed_by(f"/nix/store/other  x-deploy-1:AAA=\n", target, names), [])

    def test_v2_must_carry_the_spare_key_only(self):
        for signed in ([steps.DEPLOY_KEY, steps.SPARE_KEY], [steps.DEPLOY_KEY], []):
            with self.subTest(signed=signed), mock.patch.object(steps, "update_guard"), \
                    mock.patch.object(self.d, "deploy", return_value={"signedBy": signed}):
                self.fails(steps.step_5_3, self.d, text="spare key")


class FailedSwitches(StepHarness):
    """5.5: V4 switches and fails (exit 4, nothing set back); V5 fails at the boot loader and is reverted."""

    def scenario(self, v4=None, v5=None, after_v5=None):
        v = self.d.variants
        v4_record = {"phase": "finished", "result": "failed (exit 4)", "unexpectedUnits": ["terrace-lease-broken.service"],
                     "revert": None, "bootDefaultAfter": "6", "to": v["v4"]}
        v4_record.update(v4 or {})
        v5_record = {"phase": "finished", "result": "failed (exit 1)", "switchExit": 1, "generationAfter": "7",
                     "revert": {"from": "7", "to": "5", "result": "success"}, "bootDefaultAfter": "5", "to": v["v5"]}
        v5_record.update(v5 or {})
        started = {"phase": "started", "failedBefore": []}
        records = iter([[started, v4_record],
                        [started, {"phase": "finished", "result": "success", "action": "rollback", "to": v["v3"]}],
                        [started, v5_record]])
        systems = iter([{"current": v["v3"]}, {"current": v["v4"]}, {"current": v["v3"]},
                        after_v5 or {"current": v["v3"], "bootDefault": v["v3"]},
                        {"current": v["v3"], "booted": v["v3"], "bootDefault": v["v3"]}])

        def activate(*args, ok=(0,), timeout=1200):
            if "--dry-run" in args:
                return steps.Result(0, "Checks passed", "", 1)
            if args[0] == "rollback":
                return steps.Result(0, "Running generation 5", "", 1)
            if args[1] == v["v4"]:
                return steps.Result(1, "terrace-production: activate result: failed (exit 4)\n", "", 1)
            return steps.Result(1, "terrace-production: the switch failed before the new system ran, so the boot "
                                   "default was set back to generation 5, the running system; ...\n", "", 1)
        generations = {v["v3"]: 5, v["v4"]: 6, v["v5"]: 7}
        patches = [mock.patch.object(steps, "update_guard"),
                   mock.patch.object(self.d, "deploy", return_value={"signedBy": [steps.DEPLOY_KEY]}),
                   mock.patch.object(self.d, "activate", side_effect=activate),
                   mock.patch.object(self.d, "records", side_effect=lambda: next(records)),
                   mock.patch.object(self.d, "systems", side_effect=lambda: next(systems)),
                   mock.patch.object(self.d, "generation_of", side_effect=generations.get),
                   mock.patch.object(self.d, "no_warning"), mock.patch.object(self.d, "settled"),
                   mock.patch.object(self.d, "reboot", return_value={}),
                   mock.patch.object(self.d, "reopen_forward", return_value={})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_good_transcript_passes(self):
        self.scenario()
        observed = steps.step_5_5(self.d)
        self.assertEqual(observed["v5"]["record"]["revert"], {"from": "7", "to": "5", "result": "success"})

    def test_each_wrong_record_fails(self):
        cases = [({"v4": {"revert": {"from": "6", "to": "5", "result": "success"}}}, "no revert"),
                 ({"v4": {"unexpectedUnits": []}}, "terrace-lease-broken"),
                 ({"v4": {"bootDefaultAfter": "5"}}, "boot default"),
                 ({"v5": {"revert": {"from": "7", "to": "5", "result": "failed (exit 1)"}}}, "revert success"),
                 ({"v5": {"revert": None}}, "revert success"),
                 ({"v5": {"bootDefaultAfter": "7"}}, "boot default V3"),
                 ({"after_v5": {"current": self.d.variants["v3"], "bootDefault": self.d.variants["v5"]}},
                  "is the boot default")]
        for changes, text in cases:
            with self.subTest(changes=changes):
                self.setUp()
                self.scenario(**changes)
                self.fails(steps.step_5_5, self.d, text=text)


class AbsenceChecks(StepHarness):
    def test_status_must_answer_before_no_warning_can_pass(self):
        good = "adopted 2026-11-27T10:00:00Z\nactive\nrunning system: x\ndeploy path: on, 2 trusted deploy key(s)\n"
        self.host([(r"terrace-production status", (0, good, ""))])
        self.d.no_warning()
        for answer in ((255, "", ""), (0, "", ""), (0, "deploy path: on\n", ""), (0, good + "WARNING: x\n", ""),
                       (1, good, "")):
            with self.subTest(answer=answer[:2]):
                self.setUp()
                self.host([(r"terrace-production status", answer)])
                self.fails(self.d.no_warning)

    def test_no_busy_needs_every_unit_read(self):
        self.host([(r"systemctl show -p ActiveState", (0, "ActiveState=inactive\n" * 4, "")), (r"list-jobs", (0, "", ""))])
        self.fails(self.d.no_busy)


class Https(StepHarness):
    def test_curl_ignores_rc_files_and_every_proxy(self):
        body = json.dumps({"issuer": steps.ISSUER})
        fake = self.host([(r"identity\.terrace\.invalid", (0, body + "\n200", "")), (r"curl", (0, "{}\n200", ""))])
        for patch in (mock.patch.object(self.d, "start_background"), mock.patch.object(self.d, "alive", return_value=True),
                      mock.patch.object(self.d, "stop_background", return_value=True),
                      mock.patch.object(steps.socket, "create_connection"),
                      mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:3128", "all_proxy": "x"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(self.d.https("t")["issuer"], steps.ISSUER)
        for argv, env in fake.calls:
            self.assertEqual(argv[1:4], ["-q", "--noproxy", "*"])
            self.assertFalse({"HTTPS_PROXY", "https_proxy", "all_proxy", "ALL_PROXY"} & set(env))


class Guards(StepHarness):
    def test_stderr_is_checked_like_stdout(self):
        self.d.redactor.add_bytes(STORED_VALUE.encode(), "the backup repository password")
        self.host([(r"journalctl", (0, "fine\n", "pw " + STORED_VALUE))])
        with self.assertRaises(steps.Leak) as caught:
            self.d.ssh("journalctl -p err", 30)
        self.assertIn("the stderr of", str(caught.exception))

    def test_a_process_that_survives_is_not_verified(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        self.d.processes["second-session"] = (process, None)
        with mock.patch.object(steps, "stop_group", return_value=False):
            self.assertFalse(self.d.cleanup())
        self.assertIn("second-session not verified gone", self.d.cleanup_failures)

    def test_one_failing_clean_up_item_does_not_skip_the_rest(self):
        self.d.sftp_attempted = True
        fake = self.host([(r"rehearsal_sftp.py stop", (0, "", "")), UNITS_RULE, (r"list-units", (0, "", ""))])
        self.d.processes["reverse-forward"] = (mock.MagicMock(), None)
        with mock.patch.object(self.d, "stop_background", side_effect=OSError("boom")):
            self.assertFalse(self.d.cleanup())
        self.assertTrue(fake.ran(r"rehearsal_sftp.py stop"), "the SFTP stop still ran")
        self.assertFalse(self.d.work.exists(), "the scratch directory was still removed")

    def test_write_new_never_replaces(self):
        path = self.d.work / "x"
        steps.write_new(path, "a")
        with self.assertRaises(FileExistsError):
            steps.write_new(path, "b")
        self.assertEqual(path.read_text(), "a")


class StoreRedactor(unittest.TestCase):
    def test_store_values_are_loaded_except_short_and_public_ones(self):
        redactor = steps.Redactor()
        loaded = redactor.add_store(STORE_TREE)
        self.assertEqual(loaded, {"loaded": 2, "short": ["short"]})
        self.assertEqual(redactor.rule(b"x" + STORED_VALUE.encode()), "the store value backup/primary/repository-password")
        self.assertEqual(redactor.rule(("f" * 64).encode()), "the store value website/payload-secret")
        self.assertIsNone(redactor.rule(b"abc123 is short and ordinary"))
        self.assertIsNone(redactor.rule(b"PUBLIC CERTIFICATE BODY LINE 1234"))
        self.assertIsNone(redactor.rule(b"metadata, not a secret value of the store"))
        self.assertEqual(steps.MIN_SECRET_LENGTH, 16)

    def test_a_run_loads_the_store_before_the_first_step(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        directory = RunDirectory(tmp)
        seen = {}

        def first(d):
            seen["rule"] = d.redactor.rule(STORED_VALUE.encode())
            return {}
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"2.1": first}), mock.patch.object(steps.Driver, "cleanup", return_value=True):
            code, _, _ = quiet(steps.main, directory.argv("--steps", "2.1"))
        self.assertEqual(code, 0)
        self.assertEqual(seen["rule"], "the store value backup/primary/repository-password")
        summary = json.loads((directory.evidence / "summary.json").read_text())
        self.assertEqual(summary["redactor"], {"loaded": 2, "short": ["short"]})
        self.assertNotIn(STORED_VALUE, "".join(p.read_text() for p in directory.evidence.rglob("*") if p.is_file()))


class LateValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = RunDirectory(self.tmp)

    def refused(self, *extra, text):
        code, _, err = quiet(steps.main, self.dir.argv(*extra))
        self.assertEqual(code, steps.EXIT_INPUTS, err)
        self.assertIn(text, err)
        self.assertFalse(self.dir.evidence.exists())

    def test_a_garbage_bootstrap_key_is_refused(self):
        self.dir.private(steps.BOOTSTRAP_KEY, FAKE_PRIVATE)
        self.refused(text="ed25519 SSH private keys")

    def test_the_bootstrap_key_must_differ_from_the_operator_key(self):
        shutil.copy(self.dir.run / steps.OPERATOR_KEY, self.dir.run / steps.BOOTSTRAP_KEY)
        self.refused(text="must differ")

    def test_tools_variants_and_the_sftp_runtime(self):
        self.refused("--nix", "/nonexistent/nix", text="--nix")
        self.refused("--sftp-seconds", "20000", text="--sftp-seconds")
        with mock.patch.object(steps, "store_path_exists", return_value=False):
            self.refused(text="variant store paths missing")


class EvidenceOnEveryPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = RunDirectory(self.tmp)

    def test_an_interrupted_step_gets_its_receipt_and_the_summary_names_it(self):
        def interrupted(_d):
            raise steps.Interrupted()
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"3.2": interrupted}), \
                mock.patch.object(steps.Driver, "cleanup", return_value=True):
            code, _, _ = quiet(steps.main, self.dir.argv("--steps", "3.2"))
        self.assertEqual(code, steps.EXIT_INTERRUPTED)
        self.assertEqual(json.loads((self.dir.evidence / "receipts" / "3.2.json").read_text())["status"], "interrupted")
        self.assertEqual(json.loads((self.dir.evidence / "summary.json").read_text())["interruptedStep"], "3.2")
        self.assertIn("3.2 interrupted", (self.dir.evidence / "steps.log").read_text())
        self.assertTrue((self.dir.evidence / "SHA256SUMS").is_file())

    def test_a_receipt_that_cannot_be_written_still_ends_with_summary_and_sums(self):
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"2.1": lambda d: {}}), \
                mock.patch.object(steps.Driver, "cleanup", return_value=True), \
                mock.patch.object(steps.Evidence, "receipt", side_effect=OSError("disk full")):
            code, _, _ = quiet(steps.main, self.dir.argv("--steps", "2.1"))
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        self.assertEqual(json.loads((self.dir.evidence / "summary.json").read_text())["failedStep"], "2.1")
        self.assertTrue((self.dir.evidence / "SHA256SUMS").is_file())


class PrepareInterrupted(unittest.TestCase):
    def test_wipe_names_what_it_could_not_remove(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        scratch = tmp / "tls-x"
        scratch.mkdir()
        (scratch / "ca-key.pem").write_text("k")
        (scratch / "leaf-key.pem").write_text("k")
        os.chmod(scratch / "ca-key.pem", 0o400)
        self.addCleanup(lambda: os.chmod(scratch / "ca-key.pem", 0o600) if (scratch / "ca-key.pem").exists() else None)
        left = steps.wipe_scratch(scratch)
        if os.geteuid() != 0:
            self.assertEqual(left, ["tls-x/ca-key.pem"])
        self.assertFalse((scratch / "leaf-key.pem").exists(), "the next file was still removed")

    @unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("openssl"), "needs ssh-keygen and openssl")
    def test_sigterm_during_prepare_still_wipes_the_ca_and_leaf_keys(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        run = tmp / "run"
        run.mkdir(mode=0o700)
        archive = tmp / "a.tar.gz"
        archive.write_text("x")
        stub = tmp / "terrace-secrets"
        stub.write_text(f"""#!{sys.executable}
import os, sys
from pathlib import Path
store = Path(os.environ["TERRACE_SECRETS_STORE"]); store.mkdir(mode=0o700, exist_ok=True)
a = sys.argv[1:]
if a[:2] == ["escrow", "create"]:
    Path(a[2]).write_text("AGE-SECRET-KEY-1STUB\\n"); os.chmod(a[2], 0o600)
    (store / "escrow.recipient").write_text("age1" + "q" * 58 + "\\n")
elif a[:2] == ["host", "create"]:
    os.makedirs(a[a.index("--stage") + 1], mode=0o700)
""")
        nix = tmp / "nix"
        nix.write_text(f"""#!{sys.executable}
import sys
print(sys.argv[-1] + ":" + "E" * 86 + "==", end="") if "generate-secret" in sys.argv else sys.stdout.write("k:" + "F" * 43 + "=")
""")
        openssl = tmp / "openssl"
        # At the leaf's signature (the CA key and the leaf key exist by then) the parent gets SIGTERM.
        openssl.write_text(f"#!/bin/sh\ncase \"$*\" in *-req\\ -in*) kill -TERM $PPID; sleep 5;; esac\nexec {shutil.which('openssl')} \"$@\"\n")
        for path in (stub, nix, openssl):
            os.chmod(path, 0o755)
        argv = ["prepare", str(run), "--terrace-secrets", str(stub), "--openssl", str(openssl), "--ssh-keygen",
                shutil.which("ssh-keygen"), "--nix", str(nix), "--web-archive", str(archive), "--cms-archive", str(archive),
                "--previous-web-archive", str(archive), "--previous-cms-archive", str(archive), "--tfc-source", str(tmp),
                "--womb-revision", "1" * 40, "--key-tag", "t1"]
        code, _, err = quiet(steps.main, argv)
        self.assertEqual(code, steps.EXIT_INTERRUPTED, err)
        self.assertFalse([p for p in run.iterdir() if p.name.startswith("tls-")], "the CA and leaf keys are wiped")


# ---- Every remaining step body, scripted (review round 2) -----------------------------------------------------------

def table(rules):
    """A root()/ssh() stand-in: the first (pattern, answer) whose pattern matches the command answers; an answer is a
    Result, a (code, out) pair or a callable taking the command."""
    def answer(command, *args, ok=(0,), **kwargs):
        for pattern, value in rules:
            if re.search(pattern, command):
                value = value(command) if callable(value) else value
                result = value if isinstance(value, steps.Result) else steps.Result(value[0], value[1], "", 0.1)
                if ok is not None and result.code not in ok:
                    raise steps.Failed(f"exit {result.code}: {command}")
                return result
        raise AssertionError("unexpected command: " + command)
    return answer


class Steps(StepHarness):
    PREVIOUS = json.loads(steps.PREVIOUS_RELEASE.read_text())
    NEXT = json.loads(steps.NEXT_RELEASE.read_text())

    def patch(self, **methods):
        for name, value in methods.items():
            target = steps if name in ("update_guard", "restic_offhost", "start_record") else self.d
            effect = callable(value) or isinstance(value, BaseException)
            patch = mock.patch.object(target, name, **({"side_effect": value} if effect else {"return_value": value}))
            patch.start()
            self.addCleanup(patch.stop)

    def broken(self, step, cases, scenario):
        """Each case changes one thing in the good transcript; the step must fail on it."""
        for name, change in cases.items():
            with self.subTest(case=name):
                self.setUp()
                scenario(**change)
                self.fails(step, self.d)

    # 3.2
    def push(self, out="Installed 12 encrypted secrets.\nHost state: not adopted\n", unstaged=True,
             verify="PASS 11 secrets\n", sops="success"):
        stage = self.d.run / steps.STAGE
        stage.mkdir(mode=0o700, exist_ok=True)
        (stage / "ssh_host_ed25519_key").write_text("k")

        def unstage(_argv):
            if unstaged:
                shutil.rmtree(stage)
            return 0, "Removed the staged host key for app.\n", ""
        self.host([(r" push app --address 127\.0\.0\.1 --port 2222 --identity", (0, out, "")),
                   (r"host unstage app --stage", unstage), (r"terrace-secrets verify", (0, verify, "")),
                   (r"sops-install-secrets.service", (0, "Id=sops-install-secrets.service\nActiveState=active\n"
                                                      f"Result={sops}\nConditionResult=yes\n", ""))])

    def test_3_2(self):
        self.push()
        self.assertEqual(steps.step_3_2(self.d)["secrets"], 12)
        self.broken(steps.step_3_2, {"no Installed line": {"out": "Host state: not adopted\n"},
                                     "adopted": {"out": "Installed 12 encrypted secrets.\nHost state: adopted\n"},
                                     "stage kept": {"unstaged": False}, "verify fails": {"verify": "FAIL tls/key\n"},
                                     "sops failed": {"sops": "exit-code"}}, self.push)

    # 3.3
    def adopt(self, adopt="Adopted: the writers start now and at every boot.\n",
              status="adopted 2026-11-27T10:00:00Z\nactive\ndeploy path: on, 2 trusted deploy key(s)\n"):
        self.patch(require_forward=None, settled={})
        self.host([(r"terrace-production adopt --new", (0, adopt, "")), (r"terrace-production status", (0, status, ""))])

    def test_3_3(self):
        self.adopt()
        steps.step_3_3(self.d)
        self.broken(steps.step_3_3, {"not adopted": {"adopt": "terrace-production: not adopted yet\n"},
                                     "target inactive": {"status": "adopted 2026-11-27T10:00:00Z\ninactive\ndeploy path: on\n"}},
                    self.adopt)

    # 3.4
    def first_release(self, marker=None, ledger=None, result="success", snapshots=1):
        marker = marker or self.PREVIOUS["revision"]
        ledger = ledger if ledger is not None else self.PREVIOUS["migrations"]
        self.patch(require_forward=None, settled={}, restic_offhost={"checkReadData": True, "snapshots": 1})
        self.patch(root=table([(r"migrated-revision", (0, marker + "\n")), (r"payload_migrations", (0, "\n".join(ledger) + "\n")),
                               (r"terrace-backup.service -p Result", (0, result + "\n")),
                               (r"snapshots --json", (0, json.dumps([{"hostname": "app", "short_id": "ab12cd34"}] * snapshots))),
                               (r".", (0, ""))]))

    def test_3_4(self):
        self.first_release()
        self.assertEqual(steps.step_3_4(self.d)["snapshots"], ["ab12cd34"])
        self.broken(steps.step_3_4, {"marker": {"marker": self.NEXT["revision"]}, "ledger": {"ledger": self.NEXT["migrations"]},
                                     "backup failed": {"result": "exit-code"}, "two snapshots": {"snapshots": 2}},
                    self.first_release)

    def test_3_4_needs_the_off_host_check(self):
        self.first_release()
        with mock.patch.object(steps, "restic_offhost", side_effect=steps.Failed("restic check --read-data found errors")):
            self.fails(steps.step_3_4, self.d, text="read-data")

    # 3.5
    def test_3_5_is_the_https_check(self):
        self.patch(https={"issuer": steps.ISSUER})
        self.assertEqual(steps.step_3_5(self.d), {"issuer": steps.ISSUER})
        self.d.https.assert_called_once_with("3.5")

    # 4.1 (reboot's own wait for `running`)
    def test_a_reboot_must_reach_running(self):
        clock = iter(range(0, 10_000_000, 50))
        connection = mock.MagicMock()
        connection.__enter__.return_value.recv.return_value = b"SSH-2.0-OpenSSH"
        for patch in (mock.patch.object(steps.time, "monotonic", side_effect=lambda: next(clock)),
                      mock.patch.object(steps.socket, "create_connection", return_value=connection)):
            patch.start()
            self.addCleanup(patch.stop)
        state = {"rebooted": False}

        def boot(_argv):
            return 0, (BOOT_B if state["rebooted"] else BOOT_A) + "\n", ""

        def reboot(_argv):
            state["rebooted"] = True
            return 255, "", ""
        self.host([(r"systemctl show -p ActiveState", (0, "ActiveState=inactive\n" * 5, "")), (r"list-jobs", (0, "", "")),
                   (r"boot_id", boot), (r"systemctl reboot", reboot), (r"is-system-running --wait", (1, "degraded\n", ""))])
        self.fails(self.d.reboot, "t", text="reached running (degraded)")

    # 5.1
    def plain_switch(self, record=None, rollback=None, after_rollback=None, marker_gone=0):
        v = self.d.variants
        records = iter([[{"phase": "started"}, dict({"phase": "finished", "result": "success", "to": v["v1"]}, **(record or {}))],
                        [{"phase": "started"}, dict({"phase": "finished", "result": "success", "action": "rollback",
                                                     "to": v["v0"]}, **(rollback or {}))]])
        systems = iter([{"current": v["v1"]}, after_rollback or {"current": v["v0"]}])
        self.patch(update_guard=None, start_record={}, deploy={"signedBy": [steps.DEPLOY_KEY]}, quiesce=None,
                   stop_timers_again=None, restart_after_quiesce=None, settled={}, no_warning="",
                   activate=lambda *a, **k: steps.Result(0, "Checks passed" if "--dry-run" in a else "Running generation 2", "", 1),
                   records=lambda: next(records), systems=lambda: next(systems),
                   ssh=table([(r"cat /etc/terrace-lease-variant", (0, "v1\n")),
                              (r"test ! -e /etc/terrace-lease-variant", (marker_gone, ""))]))

    def test_5_1(self):
        self.plain_switch()
        self.assertEqual(steps.step_5_1(self.d)["rollback"]["to"], self.d.variants["v0"])
        self.broken(steps.step_5_1, {"activate failed": {"record": {"result": "failed (exit 4)"}},
                                     "rollback not recorded": {"rollback": {"action": "activate"}},
                                     "rollback to another": {"rollback": {"to": self.d.variants["v1"]}},
                                     "V1 still runs": {"after_rollback": {"current": self.d.variants["v1"]}},
                                     "marker kept": {"marker_gone": 1}}, self.plain_switch)

    # 5.2
    def release_change(self, first=None, snapshots=2, record=None, ledger_after=None, second_ledger=None, marker=None):
        v = self.d.variants
        ledger_after = ledger_after if ledger_after is not None else self.NEXT["migrations"]
        ledgers = iter([self.PREVIOUS["migrations"], ledger_after, second_ledger if second_ledger is not None else ledger_after])
        good = {"phase": "finished", "result": "switched; down until the CMS migration: terrace-cms.service terrace-web.service",
                "failedUnits": ["terrace-web.service", "terrace-cms.service"], "unexpectedUnits": [], "notStartedUnits": [],
                "waitTimedOut": False}

        def activate(*args, ok=(0,), timeout=1200):
            if "--dry-run" in args and ok == (3,):
                return first or steps.Result(3, "Every other check passed; ...\nterrace-production: dry run NOT READY: ...", "", 1)
            if "--dry-run" in args:
                return steps.Result(0, "Checks passed; activate ...", "", 1)
            return steps.Result(0, "THE SITE IS DOWN until the CMS migration: ...", "", 1)
        self.patch(update_guard=None, start_record={}, deploy={"signedBy": [steps.DEPLOY_KEY]}, require_forward=None,
                   quiesce=None, stop_timers_again=None, settled={}, https={}, failed_units=[""], activate=activate,
                   records=lambda: [{"phase": "started"}, dict(good, **(record or {}))], ledger=lambda: next(ledgers),
                   marker=marker or self.NEXT["revision"], ssh=table([(r".", (0, "active\n"))]),
                   root=table([(r"terrace-backup.service -p Result", (0, "success\n")),
                               (r"snapshots --json", (0, json.dumps([{"hostname": "app"}] * snapshots))),
                               (r"terrace-secrets verify", (0, "PASS\n")), (r".", (0, ""))]))

    def test_5_2(self):
        self.release_change()
        self.assertEqual(steps.step_5_2(self.d)["ledgerAfter"], self.NEXT["migrations"])
        self.broken(steps.step_5_2, {
            "dry run not NOT READY": {"first": steps.Result(3, "Checks passed", "", 1)},
            "backup missing": {"snapshots": 1},
            "plain success": {"record": {"result": "success"}},
            "an unexpected unit": {"record": {"unexpectedUnits": ["nginx.service"]}},
            "wait timed out": {"record": {"waitTimedOut": True}},
            "ledger not the pin's": {"ledger_after": self.PREVIOUS["migrations"]},
            "second run changed it": {"second_ledger": self.NEXT["migrations"] + ["extra"]},
            "marker old": {"marker": self.PREVIOUS["revision"]}}, self.release_change)

    # 5.3
    def initrd_change(self, refusal="initrd differ(s) from the booted system; repeat with --boot", booted=None,
                      initrd=None, warning="WARNING: the boot default, generation 4"):
        v = self.d.variants
        before = {"current": v["release"], "booted": v["release"], "bootDefault": v["release"]}
        after = booted or {"current": v["v2"], "booted": v["v2"], "bootDefault": v["v2"]}
        systems = iter([before, before, after])

        def activate(*args, ok=(0,), timeout=1200):
            if "--boot" not in args:
                return steps.Result(1, f"terrace-production: nothing changed: {refusal} (docs)", "", 1)
            return steps.Result(0, "Generation 4 is the boot default.\nReboot to run it, or cancel it with rollback.", "", 1)
        self.patch(update_guard=None, deploy={"signedBy": [steps.SPARE_KEY]}, activate=activate, systems=lambda: next(systems),
                   records=lambda: [{"phase": "started"}, {"phase": "finished", "result": "success", "mode": "boot"}],
                   status=f"adopted 2026-11-27T10:00:00Z\nactive\n{warning}\ndeploy path: on\n", reboot={}, no_warning="",
                   settled={}, reopen_forward={},
                   root=table([(r"readlink /nix/var/nix/profiles/system", (0, "system-3-link\n")), (r".", (0, "1\n"))]),
                   ssh=table([(r"readlink -f /run/booted-system/initrd", (0, initrd or "/nix/store/i-initrd\n/nix/store/i-initrd\n")),
                              (r"cat /etc/terrace-lease-variant", (0, "v2\n")), (r"df ", (0, "1 2 3\n"))]))

    def test_5_3(self):
        self.initrd_change()
        steps.step_5_3(self.d)
        self.broken(steps.step_5_3, {"other refusal": {"refusal": "is already the running system"},
                                     "booted V0": {"booted": {"current": self.d.variants["v2"], "booted": self.d.variants["v0"]}},
                                     "another initrd": {"initrd": "/nix/store/a-initrd\n/nix/store/b-initrd\n"},
                                     "no warning": {"warning": "previous generation: 3"}}, self.initrd_change)

    # 5.4
    def sshd_change(self, record=None, reconnect=0, loglevel="loglevel VERBOSE"):
        v = self.d.variants

        def background(name, argv, output=None):
            Path(output).write_text("1\n2\n3\n")
        self.patch(update_guard=None, deploy={"signedBy": [steps.DEPLOY_KEY]}, start_background=background,
                   alive=True, forward_answers=True, stop_background=True, reopen_forward={}, no_warning="",
                   activate=lambda *a, **k: steps.Result(0, "Running generation 5", "", 1),
                   records=lambda: [{"phase": "started"}, dict({"phase": "finished", "result": "success", "to": v["v3"]},
                                                               **(record or {}))],
                   ssh=table([(r"systemctl show sshd.service", (0, "101\naaaa\n")), (r"^true$", (reconnect, ""))]),
                   root=table([(r"sshd -T", (0, loglevel + "\n")), (r"journalctl", (0, "restarting the following units: sshd.service\n"))]))

    def test_5_4(self):
        self.sshd_change()
        observed = steps.step_5_4(self.d)["observed"]
        self.assertTrue(observed["sameKeyReconnects"])
        self.broken(steps.step_5_4, {"switch failed": {"record": {"result": "failed (exit 4)"}},
                                     "no reconnect": {"reconnect": 255}, "old sshd setting": {"loglevel": "loglevel INFO"}},
                    self.sshd_change)

    # 6
    def final(self, offhost=None):
        self.patch(failed_units=[""], status="adopted x\n", restic_offhost=offhost or {"checkReadData": True, "snapshots": 2},
                   root=table([(r"deployments.log", (0, json.dumps({"phase": "finished"}) + "\n")), (r".", (0, "x\n"))]),
                   ssh=table([(r"path-info -S", (0, "/run/current-system 123\n")), (r".", (0, "x\n"))]))

    def test_6(self):
        self.final()
        self.assertEqual(steps.step_6(self.d)["snapshotsOffHost"]["snapshots"], 2)
        self.setUp()
        self.final(offhost=steps.Failed("2 snapshot(s) of host app"))
        self.fails(steps.step_6, self.d)


class ClaimedBehaviours(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = RunDirectory(self.tmp)

    def test_a_store_that_cannot_be_loaded_fails_the_run(self):
        self.dir.sops.write_text("#!/bin/sh\nexit 1\n")
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"2.1": lambda d: {}}), \
                mock.patch.object(steps.Driver, "cleanup", return_value=True):
            code, _, _ = quiet(steps.main, self.dir.argv("--steps", "2.1"))
        self.assertEqual(code, steps.EXIT_STEP_FAILED)
        self.assertEqual(json.loads((self.dir.evidence / "summary.json").read_text())["failedStep"], "redactor")
        self.assertFalse((self.dir.evidence / "receipts" / "2.1.json").exists(), "no step ran")

    def test_the_first_signal_interrupts_and_later_ones_are_ignored(self):
        saved = {sig: signal.getsignal(sig) for sig in steps.SIGNALS}
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in saved.items()])
        for sig in steps.SIGNALS:
            signal.signal(sig, steps.interrupt_once)
        with self.assertRaises(steps.Interrupted):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
        self.assertTrue(all(signal.getsignal(sig) == signal.SIG_IGN for sig in steps.SIGNALS))
        os.kill(os.getpid(), signal.SIGTERM)  # ignored now

    def test_run_and_prepare_install_that_handler(self):
        seen = {}

        def capture(d):
            seen["run"] = signal.getsignal(signal.SIGTERM)
            return {}
        with mock.patch.dict(steps.STEP_FUNCTIONS, {"2.1": capture}), mock.patch.object(steps.Driver, "cleanup", return_value=True):
            quiet(steps.main, self.dir.argv("--steps", "2.1"))
        self.assertIs(seen["run"], steps.interrupt_once)

    def test_stop_group_never_reports_a_child_that_stays(self):
        process = mock.MagicMock()
        process.poll.return_value = None
        process.pid = 999999999
        process.wait.side_effect = subprocess.TimeoutExpired("x", 1)
        with mock.patch.object(steps.os, "killpg"):
            self.assertFalse(steps.stop_group(process, grace=0))

    def test_stop_group_sends_int_then_term_then_kill(self):
        process = mock.MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("x", 1)
        with mock.patch.object(steps.os, "killpg") as killpg:
            steps.stop_group(process, grace=0)
        self.assertEqual([call.args[1] for call in killpg.call_args_list], [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])

    def test_status_needs_its_deploy_path_line(self):
        StepHarness.setUp(self)
        self.addCleanup(lambda: self.d.evidence.close_step())
        with mock.patch.object(self.d, "execute", side_effect=Fake([(r"terrace-production status", (0, "adopted 2026-11-27T10:00:00Z\nactive\n", ""))])):
            with self.assertRaises(steps.Failed):
                self.d.status()

    def test_a_release_record_that_is_not_an_object_is_refused(self):
        bad = Path(self.tmp) / "release.json"
        bad.write_text("[]")
        with mock.patch.object(steps, "PREVIOUS_RELEASE", bad), mock.patch.object(steps, "ROOT", Path(self.tmp)):
            code, _, err = quiet(steps.main, self.dir.argv("--dry-run"))
        self.assertEqual(code, steps.EXIT_INPUTS)
        self.assertIn("is not a release record", err)

    def test_prepare_returns_3_when_a_scratch_file_stays(self):
        with mock.patch.object(steps, "wipe_scratch", return_value=["tls-x/ca-key.pem"]):
            where = Path(self.tmp) / "p"
            where.mkdir()
            _, code, _, err = stub_prepare(str(where))
        self.assertEqual(code, steps.EXIT_CLEANUP, err)
        self.assertIn("tls-x/ca-key.pem", err)


if __name__ == "__main__":
    unittest.main()
