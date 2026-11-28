"""Unit tests for lease_host_steps.py: inputs, selection, pinned SSH, redaction, G1 and clean-up, evidence."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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


def nix_secret(name):
    return f"{name}:{'C' * 86}==\n"


class RunDirectory:
    """A run directory with every input `run` checks, and stand-in tools."""

    def __init__(self, root):
        self.root = Path(root)
        self.run = self.root / "run"
        self.run.mkdir(mode=0o700)
        for name in (steps.BOOTSTRAP_KEY, steps.OPERATOR_KEY, steps.ESCROW):
            self.private(name, "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAAprivatekeybodyline0123456789\n-----END OPENSSH PRIVATE KEY-----\n")
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
        self.evidence = self.root / "evidence"

    def private(self, name, text):
        path = self.run / name
        path.write_text(text)
        os.chmod(path, 0o600)

    def argv(self, *extra):
        return ["run", str(self.run), "--address", "127.0.0.1", "--port", "2222", "--evidence", str(self.evidence),
                "--variants", str(self.variants), "--rclone", str(self.tool), "--restic", str(self.tool),
                "--sops", str(self.tool), "--terrace-secrets", str(self.tool), *extra]


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
            d.redactor.add_file(directory.run / steps.OPERATOR_KEY)
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
        with mock.patch.object(self.driver, "execute", return_value=(0, self.PASSWORD + b"\n", b"", 0.1)):
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
        with mock.patch.object(self.driver, "execute", return_value=(0, b"x", b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----", 0.1)):
            with self.assertRaises(steps.Leak) as caught:
                self.driver.read_secret(["sops"], 60, shown="sops decrypt")
        self.assertIn("a PEM or OpenSSH private key marker", str(caught.exception))

    def test_rules_name_their_source(self):
        redactor = steps.Redactor()
        redactor.add_file(self.dir.run / steps.OPERATOR_KEY)
        self.assertEqual(redactor.rule(b"AAAAprivatekeybodyline0123456789"), f"a line of {steps.OPERATOR_KEY}")
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

    def test_timer_guard(self):
        soon = int(time.time()) + 300
        later = int(time.time()) + 3600
        with self.answer(f"Id=terrace-backup.timer\nNextElapseUSecRealtime=@{later}\n\nId=terrace-cms-sweep.timer\n"
                         "NextElapseUSecRealtime=\n"):
            self.driver.timers_clear()
        with self.answer(f"Id=terrace-backup.timer\nNextElapseUSecRealtime=@{soon}\n"):
            with self.assertRaises(steps.Failed):
                self.driver.timers_clear()
        with self.answer("Id=terrace-backup.timer\nNextElapseUSecRealtime=Sat 2026-11-28 03:22:11 UTC\n"):
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
                d.sftp_started = True
                self.assertTrue(d.cleanup())
            self.assertIn("stop", execute.call_args[0][0])
        finally:
            shutil.rmtree(tmp)


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


if __name__ == "__main__":
    unittest.main()
