"""Offline proof of the production lease driver (rehearsal_lease_driver.py).

A fake tool layer stands in for every tool: it records each call with its exact argv and environment, writes the
receipts the real tool would write, and can fail, hang, deliver a real signal to this process, or hand a step to the
REAL Runner with a real child process (a sleeper past its bound, a child trapping SIGTERM, a grandchild holding the
pipe). Nothing here contacts a provider or a host. Run: python3 -B infra/cloud/test_rehearsal_lease_driver.py -v
"""

import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import rehearsal_apply as apply
import rehearsal_lease as lease
import rehearsal_lease_driver as driver

ROOT = Path(__file__).resolve().parents[2]
RUN = "c0ffee" + "0" * 26
REV = "1234567890abcdef1234567890abcdef12345678"
STORE_HASH = "0123456789abcdfghijklmnpqrsvwxyz"[:32]
KEXEC = f"/nix/store/{STORE_HASH}-terrace-kexec-tarball"
SYSTEM = f"/nix/store/{'a' * 32}-nixos-system-app-26.05"
DISKO = f"/nix/store/{'b' * 32}-disko"
INSTALLER = f"/nix/store/{'c' * 32}-nixos-install"
VARIANTS = {"v0": SYSTEM, **{name: f"/nix/store/{letter * 32}-nixos-system-app-{name}"
                             for name, letter in zip(("v1", "release", "v2", "v3", "v4", "v5"), "dfghij")}}
ADDRESS = "203.0.113.7"
FIXED = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc).timestamp()
USER = "operator-test"
HOST_LIST = b"2.1  first-boot window\n3.1  throwaway SFTP endpoint and reverse forward\n6  final evidence\n"
PLANTED = {"token": "PLANTEDtokenVALUE0123456789abcdefXYZ", "bootstrap": "PLANTED-BOOTSTRAP-PRIVATE-KEY-material",
           "deploy": "PLANTED-DEPLOY-SECRET-material", "spare": "PLANTED-SPARE-SECRET-material",
           "escrow": "AGE-SECRET-KEY-1PLANTEDESCROWVALUE", "hostkey": "PLANTED-HOST-OPERATOR-PRIVATE"}
BASELINE = {kind: {} for kind in lease.INVENTORY}
BASELINE.update(servers={"1": {"name": "existing"}}, primary_ips={"2": {"name": "a"}, "3": {"name": "b"}},
                ssh_keys={"4": {"name": "k"}})
COUNTS = {kind: len(rows) for kind, rows in BASELINE.items()}
SYSTEMCTL = "/run/current-system/sw/bin/systemctl"
SYSTEMD_RUN = "/run/current-system/sw/bin/systemd-run"
FLAGS = ["-E", "-s", "-B"]
WRITERS = ("foundation-preview", "foundation-apply", "original-preview", "original-apply", "original-app-apply")
SECRETS = ("operator-ed25519", "deploy-key.sec", "spare-key.sec", "escrow.age", "host-operator-ed25519", "secrets",
           "host-stage", "original-app-bootstrap-nonce", "foundation/passphrase.json", "original/passphrase.json",
           "run-ca.pem")


class FakeTools:
    """The fake tool layer. Each call is recorded as (label, argv); environments are kept in `envs`."""

    def __init__(self, case):
        self.case = case
        self.calls = []
        self.envs = []
        self.cwds = []
        self.fail = {}          # label -> exit code
        self.signal_at = {}     # label -> signal(s) sent to this process while the tool "runs"
        self.after_fail = set()  # labels whose side effects happen before the non-zero exit
        self.hang = set()       # labels that block until abort_current (0 if never aborted)
        self.real = {}          # label -> argv run by the REAL Runner instead
        self.aborted = threading.Event()
        self.overrides = {}
        self.real_runner = driver.Runner()

    def run(self, argv, timeout, *, label, foreground=True, grace=30, cwd=None, env=None, interrupted=None):
        self.calls.append((label, list(argv)))
        self.envs.append(env)
        self.cwds.append(cwd)
        if label in self.real:
            outcome = self.real_runner.run(self.real[label], timeout, label=label, grace=grace, env=None,
                                           interrupted=interrupted)
            if outcome.code == 0 and outcome.reason is None:
                self.effect(label, argv)
            return outcome
        if label in self.signal_at:
            for sig in (self.signal_at[label] if isinstance(self.signal_at[label], tuple) else (self.signal_at[label],)):
                os.kill(os.getpid(), sig)
                time.sleep(0.1)
        if label in self.hang:
            aborted = self.aborted.wait(3)
            return (-15, b"terminated\n") if aborted else (0, b"ran to the end\n")
        if label in self.fail:
            if label in self.after_fail:
                self.effect(label, argv)
            return self.fail[label], b"refused\n"
        if label in self.overrides:
            return self.overrides[label](argv)
        return self.effect(label, argv)

    def abort_current(self):
        self.aborted.set()

    def settle(self, seconds):
        return True

    def effect(self, label, argv):
        c = self.case
        R = c.R
        if label == "plan-host-steps-list":
            return 0, HOST_LIST
        if label == "plan-kexec-eval":
            return 0, (KEXEC + "\n").encode()
        if label == "plan-store-roots":
            target = argv[-1]
            link = c.R / "lease" if target.endswith("install.json") else c.R / "variants"
            return 0, f"{link} -> {target}\n".encode()
        if label == "idle-units":
            return 0, b""
        if label in ("7.1-wait", "7.1-show", "7.5-show", "7.6-show", "resume-units"):
            return 0, b"inactive\n"
        if label == "resume-load":
            return 0, b"not-found\n"
        if label == "7.7-timer":
            return 0, b"active\n"
        if label == "0.6a":
            key = Path(argv[-1])
            key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n" + PLANTED["bootstrap"] + "\n")
            key.chmod(0o600)
            Path(str(key) + ".pub").write_text("ssh-ed25519 " + "A" * 68 + " terrace-lease-bootstrap\n")
        elif label == "0.8a":
            c.planted.append((R / "original-app-bootstrap-nonce").read_text().strip())
        elif label == "0.8b":
            lease.write_private(R / "preflight/lease.json", {"schema": 1, "run": argv[argv.index("--run") + 1],
                                                             "baseline": c.preflight_baseline})
        elif label == "1.1a":
            lease.write_private(R / "lease.json", {"schema": 1, "run": RUN, "credentialSha256": "0" * 64,
                                                   "expires": int(argv[argv.index("--expires") + 1]),
                                                   "baseline": c.lease_baseline})
        elif label == "1.1b":
            lease.write_private(R / "timer-armed.json", {"run": RUN})
        elif label in ("1.2a", "1.3c"):
            phase = "foundation" if label == "1.2a" else "original"
            targets = apply.phase_targets(phase, RUN, single_app=phase == "original")
            (R / phase).mkdir(mode=0o700, exist_ok=True)
            lease.write_private(R / phase / "review.json", {"creates": sorted(targets),
                                                            "roles": [] if phase == "foundation" else ["app"],
                                                            "sourceSha256": c.source_sha})
            lease.write_private(R / phase / "passphrase.json", {"value": PLANTED["token"][::-1]})
        elif label == "1.2b":
            lease.write_private(R / "foundation-created.json",
                                {"servers": [], "primary_ips": [], "firewalls": [11], "ssh_keys": [12]})
        elif label == "1.3d":
            lease.write_private(R / "original-dispatched.json", {"run": RUN})
            lease.write_private(R / "original-created.json",
                                {"servers": [21], "primary_ips": [22], "firewalls": [], "ssh_keys": []})
        elif label in ("1.4", "1.7c", "1.4-refresh"):
            lease.write_private(R / f"original-app-binding-{time.time_ns()}.json", {
                "run": RUN, "phase": "original", "role": "app", "serverId": 21, "ipv4": ADDRESS,
                "expires": int(FIXED) + 14400, "observedAt": int(time.time())})
        elif label == "1.6a":
            path = R / "original-app-install.json"
            lease.write_private(path, {"diskSha256": "e" * 64, "system": SYSTEM, "disko": DISKO, "installer": INSTALLER})
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return 0, f"Reviewed install plan and installed host key recorded; approve with --install-approved-sha {digest}\n".encode()
        elif label == "1.6b":
            lease.write_private(R / "original-app-installed.json", {
                "binding": {"run": RUN, "phase": "original", "role": "app", "serverId": 21, "ipv4": ADDRESS,
                            "expires": int(FIXED) + 14400}, "system": SYSTEM, "mountedExt4Verified": True})
        elif label == "2.1-6":
            (R / "host-steps").mkdir(mode=0o700, exist_ok=True)
            (R / "sftp" / "sftp-listener.json").write_text("{}")
            if not c.sftp_left_running:
                (R / "sftp" / "sftp-stopped.json").write_text("{}")
        elif label == "7.3":
            lease.write_private(R / "closed.json", {"closedAt": 1})
            lease.write_private(R / f"final-absence-{time.time_ns()}.json", {
                "run": RUN, "phase": "final", "baselinePreserved": True, "absenceReads": c.absence_reads,
                "unresolvedDispatches": []})
        elif label == "7.4":
            audit = Path(argv[argv.index("--directory") + 1])
            lease.write_private(audit / "lease.json", {"schema": 1, "baseline": c.audit_baseline})
        return 0, b"ok\n"


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-driver-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        original_root = driver.running_as_root
        driver.running_as_root = lambda: False  # CI runs these tests as root; the refusal has its own test
        self.addCleanup(setattr, driver, "running_as_root", original_root)
        self.planted = list(PLANTED.values())
        self.audit_baseline = BASELINE
        self.preflight_baseline = BASELINE
        self.lease_baseline = BASELINE
        self.absence_reads = 3
        self.sftp_left_running = False
        self.leases = self.tmp / "leases"
        self.leases.mkdir(mode=0o700)
        self.R = self.leases / RUN
        self.R.mkdir(mode=0o700)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("pulumi-bin", "node-bin"):
            (self.tmp / name).mkdir()
        self.unit_path = f"{self.tmp / 'pulumi-bin'}:{self.tmp / 'node-bin'}"
        self.sbin = self.tmp / "sbin"  # outside --child-path: only the tool digest can see a changed sudo
        self.sbin.mkdir()
        (self.sbin / "sudo").write_text("#!/bin/sh\nexit 0\n")
        (self.sbin / "sudo").chmod(0o755)
        for name in ("ssh-keygen", "nix", "nix-store", "rclone", "restic", "sops", "terrace-secrets",
                     "queue-idle"):
            (self.bin / name).write_text("#!/bin/sh\nexit 0\n")
            (self.bin / name).chmod(0o755)
        self.source = self.make_source()
        self.source_sha = apply.fingerprint_source(lambda path: (self.source / "infra/cloud" / path).read_bytes())
        self.make_prepare_layout()
        self.make_store()
        self.stage()
        self.manifest = driver.tree_manifest(self.source)
        self.tools = FakeTools(self)
        self.out, self.err = io.StringIO(), io.StringIO()

    def make_source(self):
        source = self.tmp / "source"
        for relative in ["infra/cloud/rehearsal_lease.py", *(f"infra/cloud/{name}" for name in apply.PARENT_FILES),
                         *(f"infra/cloud/rehearsal-create/{name}" for name in apply.PROJECT_FILES),
                         "infra/nixos/kexec-installer.nix"]:
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, source / relative)
        (source / "infra/nixos/lease_host_steps.py").write_text("# stand-in for the host-steps driver\n")
        return source

    def private(self, path, text, mode=0o600):
        path.write_text(text)
        path.chmod(mode)

    def make_prepare_layout(self):
        R = self.R
        self.private(R / "provider-token", PLANTED["token"])
        self.private(R / "host-operator-ed25519", PLANTED["hostkey"])
        self.private(R / "host-operator-ed25519.pub", "ssh-ed25519 " + "B" * 68 + "\n", 0o644)
        self.private(R / "deploy-key.sec", PLANTED["deploy"])
        self.private(R / "deploy-key.pub", "deploy:pub\n", 0o644)
        self.private(R / "spare-key.sec", PLANTED["spare"])
        self.private(R / "spare-key.pub", "spare:pub\n", 0o644)
        self.private(R / "escrow.age", PLANTED["escrow"])
        self.private(R / "run-ca.pem", "-----BEGIN CERTIFICATE-----\n", 0o644)
        self.private(R / "sftp-client.pub", "ssh-ed25519 " + "C" * 68 + "\n", 0o644)
        self.private(R / "lease-inputs.json", json.dumps({"wombRevision": REV, "disk": "/dev/sda"}), 0o644)
        self.private(R / "lease-inputs.nix", "{ }\n", 0o644)
        (R / "secrets/hosts/app").mkdir(parents=True, mode=0o700)
        self.private(R / "secrets/hosts/app/ssh_host_ed25519_key.pub", "ssh-ed25519 " + "D" * 68 + "\n", 0o644)
        (R / "host-stage").mkdir(mode=0o700)
        self.private(R / "host-stage/ssh_host_ed25519_key", PLANTED["hostkey"] + "-stage")
        (R / "sftp").mkdir(mode=0o700)
        self.private(R / "sftp/sftp-host-key", PLANTED["hostkey"] + "-sftp")

    def make_store(self):
        self.store = self.tmp / "nix/store"
        (self.store / f"{STORE_HASH}-terrace-kexec-tarball").mkdir(parents=True)
        self.archive = self.store / f"{STORE_HASH}-terrace-kexec-tarball/nixos-kexec-installer-x86_64-linux.tar.gz"
        self.archive.write_bytes(b"kexec archive bytes")
        install = self.store / f"{'k' * 32}-lease-install.json"
        install.write_text(json.dumps({"schema": 1, "role": "app", "device": "/dev/sda", "system": SYSTEM,
                                       "disko": DISKO, "installer": INSTALLER}))
        variants = self.store / f"{'l' * 32}-lease-variants.json"
        variants.write_text(json.dumps(VARIANTS))
        (self.R / "lease").symlink_to(install)
        (self.R / "variants").symlink_to(variants)

    def stage(self):
        def bun(argv, timeout, *, label, cwd=None, **_):
            modules = Path(cwd) / "node_modules/@pulumi"
            for name in ("hcloud", "pulumi"):
                (modules / name).mkdir(parents=True)
            return 0, b""
        runner = type("R", (), {"run": staticmethod(bun)})()
        self.assertEqual(driver.stage_project(self.R, self.source, "/fake/bun", runner), apply.fingerprint_source(
            lambda path: (self.source / "infra/cloud" / path).read_bytes()))

    def config(self, **changes):
        b = str(self.bin)
        values = dict(run_directory=self.R, source=self.source, python=sys.executable, revision=REV,
                      manifest_sha256=self.manifest, ssh_keygen=b + "/ssh-keygen", nix=b + "/nix",
                      nix_store=b + "/nix-store", sudo=str(self.sbin / "sudo"), kexec_archive=self.archive,
                      kexec_sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(), ssh_cidr="198.51.100.9/32",
                      unit_path=self.unit_path, child_path=f"{b}:/usr/bin:/bin", rclone=b + "/rclone",
                      restic=b + "/restic", sops=b + "/sops", terrace_secrets=b + "/terrace-secrets",
                      expect_baseline=COUNTS, min_free_bytes=0, store_dir=str(self.store), user=USER)
        values.update(changes)
        return driver.Config(**values)

    def plan(self, cfg=None, start=FIXED):
        cfg = cfg or self.config()
        plan = driver.make_plan(cfg, self.tools, start)
        path = self.R / "lease-plan.json"
        if path.exists():
            path.unlink()
        lease.write_private(path, plan)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def drive(self, cfg=None, now=FIXED):
        cfg = cfg or self.config()
        digest = self.plan(cfg)
        self.tools.calls.clear()
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: now, sleep=lambda _: None)
        self.driver = d
        self.code = d.run(digest)
        return self.code

    def labels(self):
        return [label for label, _ in self.tools.calls]

    def py(self, *parts):
        return [sys.executable, *FLAGS, str(self.source.joinpath(*parts))]

    def unit(self, name, argv):
        return [str(self.sbin / "sudo"), "-n", SYSTEMD_RUN, "--quiet", "--collect", "--wait", "--pipe",
                f"--unit=terrace-cloud-{RUN}-{name}.service", "--uid=" + USER, "--service-type=exec",
                "--property=KillMode=control-group", "--property=RuntimeMaxSec=230", "--property=TimeoutStopSec=10s",
                "--property=UMask=0077", "--property=NoNewPrivileges=yes", "--setenv=PATH=" + self.unit_path, *argv]

    def assert_teardown_complete(self):
        self.assertEqual(self.labels()[-len(TEARDOWN_LABELS):], TEARDOWN_LABELS)


def wait_for(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def normalize(argv):
    text = json.dumps(argv)
    text = re.sub(r"audit-[0-9]+", "audit-<NS>", text)
    text = re.sub(r'"--run", "(?!' + RUN + r')[0-9a-f]{32}"', '"--run", "<FRESH>"', text)
    return json.loads(text)


PLAN_LABELS = ["plan-host-steps-list", "plan-kexec-eval", "plan-store-validity", "plan-store-roots",
               "plan-store-roots", "idle-units", "idle-units"]
RUN_LABELS = PLAN_LABELS + [
    "idle-units", "idle-units", "0.6a", "0.8a", "0.8b", "1.1a", "1.1b", "1.1c", "1.2a", "1.2b", "1.3b", "1.3c", "1.3d",
    "1.4", "1.5a", "1.5b", "1.5c", "1.5d", "1.6a", "1.6b", "1.7a", "1.7b", "1.7c", "2.0", "2.1-6"]
TEARDOWN_LABELS = [*["7.1-wait"] * 5, "7.1", *["7.1-show"] * 5, "7.3", "7.4", *["7.5-show"] * 5, "7.6",
                   *["7.6-show"] * 2, "idle-units", "idle-units"]
NOT_ARMED_LABELS = ["idle-units", "idle-units"]


class HappyPath(Harness):
    def test_exact_call_sequence_with_arguments(self):
        self.assertEqual(self.drive(), driver.EXIT_PASS, self.err.getvalue())
        R, T, S = str(self.R), str(self.R / "provider-token"), self.source
        b = str(self.bin)
        creds = ["--directory", R, "--credential-file", T]
        lease_tool, timer_tool = self.py("infra/cloud/rehearsal_lease.py"), self.py("infra/cloud/rehearsal_timer.py")
        apply_tool = self.py("infra/cloud/rehearsal_apply.py")
        kexec = self.py("infra/nixos/stock_kexec.py") + [R, "original", "app"]
        availability = lease_tool + ["availability", *creds, "--server-type", "cpx42", "--location", "nbg1"]
        binding = apply_tool + ["binding", "original", *creds, "--role", "app"]
        expires = str(int(FIXED) + 14400)
        idle = [[SYSTEMCTL, "list-units", "--plain", "--no-legend", "terrace-cloud-*", "terrace-lease-sftp-*"],
                [SYSTEMCTL, "--user", "list-units", "--plain", "--no-legend", "terrace-cloud-*", "terrace-lease-sftp-*"]]

        def show(label, unit):
            return (label, [SYSTEMCTL, "show", unit, "--property=ActiveState", "--value"])
        writers = [f"terrace-cloud-{RUN}-{u}.service" for u in WRITERS]
        expected = [
            ("plan-host-steps-list", self.py("infra/nixos/lease_host_steps.py") + ["list"]),
            ("plan-kexec-eval", [b + "/nix", "eval", "--raw", "--offline", "--impure", "--file",
                                 str(S / "infra/nixos/kexec-installer.nix"), "archive.outPath"]),
            ("plan-store-validity", [b + "/nix-store", "--check-validity", SYSTEM, DISKO, INSTALLER,
                                     *(VARIANTS[n] for n in ("v1", "release", "v2", "v3", "v4", "v5"))]),
            ("plan-store-roots", [b + "/nix-store", "--query", "--roots", os.path.realpath(self.R / "lease")]),
            ("plan-store-roots", [b + "/nix-store", "--query", "--roots", os.path.realpath(self.R / "variants")]),
            ("idle-units", idle[0]), ("idle-units", idle[1]),
            ("idle-units", idle[0]), ("idle-units", idle[1]),
            ("0.6a", [b + "/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "terrace-lease-bootstrap", "-f",
                      R + "/operator-ed25519"]),
            ("0.8a", availability),
            ("0.8b", lease_tool + ["prepare", "--directory", R + "/preflight", "--credential-file", T,
                                   "--run", "<FRESH>", "--expires", str(int(FIXED) + 1800)]),
            ("1.1a", lease_tool + ["prepare", *creds, "--run", RUN, "--expires", expires]),
            ("1.1b", timer_tool + ["arm", *creds]),
            ("1.1c", timer_tool + ["ready", *creds]),
            ("1.2a", self.unit("foundation-preview", apply_tool + ["preview", "foundation", *creds])),
            ("1.2b", self.unit("foundation-apply", apply_tool + [
                "create", "foundation", *creds, "--review-sha",
                hashlib.sha256((self.R / "foundation/review.json").read_bytes()).hexdigest(),
                "--source-sha", self.source_sha])),
            ("1.3b", availability),
            ("1.3c", self.unit("original-preview", apply_tool + ["preview", "original", *creds])),
            ("1.3d", self.unit("original-apply", apply_tool + [
                "create", "original", *creds, "--review-sha",
                hashlib.sha256((self.R / "original/review.json").read_bytes()).hexdigest(),
                "--source-sha", self.source_sha])),
            ("1.4", binding),
            ("1.5a", kexec + ["--pin-proof"]),
            ("1.5b", kexec + ["--dispatch", str(self.archive), hashlib.sha256(b"kexec archive bytes").hexdigest()]),
            ("1.5c", kexec + ["--verify-ram"]),
            ("1.5d", kexec + ["--inspect-target", "320"]),
            ("1.6a", self.py("infra/nixos/lease_install_plan.py") + [
                R, "original", "app", "--outputs", R + "/lease", "--host-stage", R + "/host-stage",
                "--host-public", R + "/secrets/hosts/app/ssh_host_ed25519_key.pub"]),
            ("1.6b", self.py("infra/nixos/rescue_install.py") + [
                R, "original", "app", "--install-approved-sha",
                hashlib.sha256((self.R / "original-app-install.json").read_bytes()).hexdigest()]),
            ("1.7a", self.unit("original-app-apply", self.py("infra/cloud/rehearsal_provider_actions.py") + [
                "boot-installed", "original", "app", *creds])),
            ("1.7b", self.py("infra/cloud/rehearsal_lease_driver.py") + ["wait-tcp", ADDRESS, "--seconds", "300"]),
            ("1.7c", binding),
            ("2.0", self.py("infra/nixos/lease_host_steps.py") + ["pin", R, "--address", ADDRESS]),
            ("2.1-6", self.py("infra/nixos/lease_host_steps.py") + [
                "run", R, "--address", ADDRESS, "--evidence", R + "/host-evidence", "--variants", R + "/variants",
                "--rclone", b + "/rclone", "--restic", b + "/restic", "--sops", b + "/sops",
                "--terrace-secrets", b + "/terrace-secrets", "--nix", b + "/nix"]),
            *(show("7.1-wait", unit) for unit in writers),
            ("7.1", [str(self.sbin / "sudo"), "-n", SYSTEMCTL, "stop", *writers]),
            *(show("7.1-show", unit) for unit in writers),
            ("7.3", lease_tool + ["cleanup", *creds]),
            ("7.4", lease_tool + ["prepare", "--directory", R + "/audit-<NS>", "--credential-file", T, "--run",
                                  "<FRESH>", "--expires", str(int(FIXED) + 900)]),
            *(show("7.5-show", unit) for unit in writers),
            ("7.6", timer_tool + ["disarm", *creds]),
            show("7.6-show", f"terrace-cloud-{RUN}.timer"), show("7.6-show", f"terrace-cloud-{RUN}.service"),
            ("idle-units", idle[0]), ("idle-units", idle[1]),
        ]
        self.assertEqual([(label, normalize(argv)) for label, argv in self.tools.calls], expected)

    def test_children_get_only_the_stated_environment(self):
        self.drive()
        expected = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": None, "USER": USER, "LOGNAME": USER,
                    "LANG": "C.UTF-8", "TZ": "UTC"}
        for env in self.tools.envs:
            self.assertEqual(set(env), set(expected))
            self.assertEqual({k: v for k, v in env.items() if k != "HOME"},
                             {k: v for k, v in expected.items() if k != "HOME"})

    def test_pass_closes_the_lease_and_deletes_material(self):
        self.assertEqual(self.drive(), driver.EXIT_PASS)
        closing = json.loads((self.R / "lease-closed.json").read_text())
        self.assertEqual((closing["exit"], closing["failedStep"], closing["state"]["timer"], closing["problems"]),
                         (0, None, "disarmed", []))
        for name in ("provider-token", *SECRETS, "sftp", "host-steps"):
            self.assertFalse(os.path.lexists(self.R / name), name)
        self.assertFalse((self.leases / (RUN + ".open")).exists())
        self.assertTrue((self.R / "driver/SHA256SUMS").exists())
        self.assertIn("2.1-6 passed exit 0", (self.R / "driver/steps.log").read_text())
        inputs = json.loads((self.R / "original-input.json").read_text())
        self.assertEqual({k: inputs[k] for k in ("roles", "appType", "location", "image", "sshKeyId", "firewallId")},
                         {"roles": ["app"], "appType": "cpx42", "location": "nbg1", "image": "debian-13",
                          "sshKeyId": 12, "firewallId": 11})
        foundation = json.loads((self.R / "foundation-input.json").read_text())
        self.assertEqual(foundation["sshCidrs"], ["198.51.100.9/32"])
        self.assertEqual(inputs["bootstrapOperatorPublicKey"], foundation["sshPublicKey"])

    def test_no_retry_anywhere(self):
        self.drive()
        self.assertEqual(self.labels(), RUN_LABELS + TEARDOWN_LABELS)
        for label in RUN_LABELS[len(PLAN_LABELS) + 2:] + ["7.1", "7.3", "7.4", "7.6"]:
            self.assertEqual(self.labels().count(label), 1, label)


class Secrets(Harness):
    def assert_no_planted(self):
        texts = [json.dumps(self.tools.calls), json.dumps(self.tools.envs), self.out.getvalue(), self.err.getvalue()]
        for path in self.R.rglob("*"):
            if path.is_file() and not path.is_symlink() and "secrets" not in path.parts and "sftp" not in path.parts \
                    and path.name not in ("provider-token", "operator-ed25519", "host-operator-ed25519",
                                          "deploy-key.sec", "spare-key.sec", "escrow.age",
                                          "original-app-bootstrap-nonce", "ssh_host_ed25519_key", "passphrase.json"):
                texts.append(path.read_text(errors="replace"))
        joined = "\n".join(texts)
        for value in self.planted:
            self.assertNotIn(value, joined)

    def test_no_secret_in_argv_env_logs_or_receipts_on_pass(self):
        self.drive()
        self.assert_no_planted()

    def test_no_secret_on_failure_or_escalation(self):
        self.tools.fail["1.6b"] = 1
        self.tools.fail["7.3"] = 1
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assert_no_planted()


class FailureAtEveryStep(Harness):
    def run_failing(self, step):
        if not step.internal:
            self.tools.fail[step.id] = 3 if step.host else 1
            return self.drive()
        original = getattr(driver.Driver, step.internal)
        setattr(driver.Driver, step.internal, lambda self: (_ for _ in ()).throw(driver.Failed("injected")))
        try:
            return self.drive()
        finally:
            setattr(driver.Driver, step.internal, original)

    def test_each_step_failure_stops_and_tears_down(self):
        steps = driver.run_steps(self.config())
        ids = [s.id for s in steps]
        for index, step in enumerate(steps):
            with self.subTest(step=step.id):
                self.setUp()
                code = self.run_failing(step)
                labels = self.labels()[len(PLAN_LABELS):]
                self.assertFalse(set(ids[index + 1:]) & set(labels), labels)
                if index > ids.index("1.1a"):
                    self.assert_teardown_complete()
                    self.assertEqual(code, driver.EXIT_ESCALATE if step.host else driver.EXIT_STEP_FAILED)
                else:
                    self.assertNotIn("7.3", labels)
                    self.assertEqual(labels[-2:], NOT_ARMED_LABELS)
                    self.assertEqual(code, driver.EXIT_STEP_FAILED)
                    for name in ("provider-token", *SECRETS):
                        self.assertFalse(os.path.lexists(self.R / name), name)  # (iii) deleted before "clean"
                for label in set(labels) - {"idle-units", "7.1-wait", "7.1-show", "7.5-show", "7.6-show"}:
                    self.assertEqual(labels.count(label), 1, label)

    def test_each_after_guard_failure_stops_and_tears_down(self):
        for step in [s for s in driver.run_steps(self.config()) if s.after]:
            with self.subTest(step=step.id):
                self.setUp()
                original = getattr(driver.Driver, step.after)
                setattr(driver.Driver, step.after, lambda self, output: (_ for _ in ()).throw(driver.Failed("guard")))
                try:
                    code = self.drive()
                finally:
                    setattr(driver.Driver, step.after, original)
                self.assertEqual(self.labels()[-1], "idle-units")
                if step.id == "0.8b":
                    self.assertNotIn("1.1a", self.labels())
                    self.assertNotIn("7.3", self.labels())
                else:
                    self.assertIn("7.3", self.labels())
                self.assertEqual(code, driver.EXIT_STEP_FAILED)

    def test_preflight_drift_from_the_stated_baseline_stops_before_arming(self):
        self.preflight_baseline = dict(BASELINE, servers={})
        self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        self.assertNotIn("1.1a", self.labels())
        self.assertIn("drift", json.loads((self.R / "driver/receipts/0.8b.json").read_text())["error"])

    def test_lease_baseline_must_equal_the_preflight(self):
        # Two reads that disagree are for a person: the final audit compares them, so the timer stays armed.
        self.lease_baseline = dict(BASELINE, ssh_keys={})
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertNotIn("1.1b", self.labels())
        self.assertIn("7.3", self.labels())
        self.assertNotIn("7.6", self.labels())

    def test_prepare_that_wrote_its_manifest_then_failed_is_torn_down(self):
        self.tools.fail["1.1a"] = 1
        self.tools.after_fail.add("1.1a")
        self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        self.assert_teardown_complete()
        self.assertNotIn("1.1b", self.labels())

    def test_host_steps_exit_codes_keep_their_meaning(self):
        for exit_code, expected in ((1, driver.EXIT_STEP_FAILED), (2, driver.EXIT_STEP_FAILED),
                                    (3, driver.EXIT_ESCALATE), (4, driver.EXIT_STEP_FAILED),
                                    (124, driver.EXIT_ESCALATE), (-9, driver.EXIT_ESCALATE)):
            with self.subTest(exit=exit_code):
                self.setUp()
                self.tools.fail["2.1-6"] = exit_code
                self.assertEqual(self.drive(), expected)
                receipt = json.loads((self.R / "driver/receipts/2.1-6.json").read_text())
                self.assertEqual(receipt["hostExitMeaning"],
                                 driver.HOST_EXIT_MEANING.get(exit_code, "no status or killed: unverified"))
                self.assertIn("7.6", self.labels())  # the provider side is still verified and disarmed

    def test_host_steps_unverified_keeps_local_state_and_deletes_secrets(self):
        self.tools.fail["2.1-6"] = 3
        self.tools.after_fail.add("2.1-6")
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        for name in ("provider-token", *SECRETS):
            self.assertFalse(os.path.lexists(self.R / name), name)
        self.assertTrue((self.R / "sftp").is_dir())
        self.assertTrue((self.R / "host-steps").is_dir())
        self.assertIn("timer terrace-cloud-", self.err.getvalue())
        self.assertIn("disarmed", self.err.getvalue())
        self.assertIn("rehearsal_sftp.py status", self.err.getvalue())

    def test_a_failed_sftp_stop_escalates_and_keeps_the_endpoint_directory(self):
        self.sftp_left_running = True
        self.tools.fail["7.2"] = 1
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertIn("7.2", self.labels())
        self.assertTrue((self.R / "sftp").is_dir())
        self.assertFalse((self.R / "provider-token").exists())
        self.assertIn("SFTP endpoint is not verified stopped", self.err.getvalue())

    def test_a_crashing_child_is_a_failure(self):
        self.tools.fail["1.5b"] = -9
        self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        self.assertNotIn("1.5c", self.labels())
        self.assertIn("7.3", self.labels())

    def test_driver_crash_still_tears_down(self):
        original = driver.Driver.after_binding
        driver.Driver.after_binding = lambda self, output: 1 / 0
        try:
            self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        finally:
            driver.Driver.after_binding = original
        self.assert_teardown_complete()

    def test_deadline_guard_stops_before_a_step_that_cannot_finish(self):
        cfg = self.config()
        digest = self.plan(cfg)
        self.tools.calls.clear()
        clock = {"now": FIXED}
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: clock["now"], sleep=lambda _: None)
        original = self.tools.effect

        def late(label, argv):
            result = original(label, argv)
            if label == "1.5d":
                clock["now"] = FIXED + 14400 - 1500 - 2000  # 2000 s left before the reserve; install needs 2700
            return result
        self.tools.effect = late
        self.assertEqual(d.run(digest), driver.EXIT_STEP_FAILED)
        self.assertIn("1.6a", self.labels())
        self.assertNotIn("1.6b", self.labels())
        self.assertIn("deadline", json.loads((self.R / "driver/receipts/1.6b.json").read_text())["error"])


class Interrupts(Harness):
    def interrupt(self, label, sig=signal.SIGTERM):
        self.tools.signal_at[label] = sig
        return self.drive()

    def test_signal_handler_never_raises(self):
        d = driver.Driver(self.config(), self.tools, out=self.out, err=self.err)
        for _ in range(3):
            self.assertIsNone(d.on_signal(signal.SIGTERM, None))
        self.assertTrue(d.interrupted)
        self.assertEqual(d.signals, [signal.SIGTERM] * 3)
        self.assertEqual((self.out.getvalue(), self.err.getvalue()), ("", ""))  # the handler writes nothing

    def test_interrupt_before_anything_is_armed(self):
        self.assertEqual(self.interrupt("0.8a", signal.SIGINT), driver.EXIT_INTERRUPTED)
        self.assertNotIn("7.3", self.labels())
        self.assertNotIn("0.8b", self.labels())
        self.assertFalse((self.leases / (RUN + ".open")).exists())
        self.assertTrue((self.R / "lease-closed.json").exists())
        self.assertFalse((self.R / "provider-token").exists())

    def test_interrupt_at_every_armed_phase_tears_down(self):
        for label, sig in (("1.1b", signal.SIGHUP), ("1.1c", signal.SIGTERM), ("1.2b", signal.SIGINT),
                           ("1.3d", signal.SIGTERM), ("1.5c", signal.SIGHUP), ("1.6b", signal.SIGINT),
                           ("1.7a", signal.SIGTERM), ("2.1-6", signal.SIGTERM)):
            with self.subTest(label=label):
                self.setUp()
                self.assertEqual(self.interrupt(label, sig), driver.EXIT_INTERRUPTED)
                labels = self.labels()
                self.assertEqual(labels[labels.index(label) + 1], "7.1-wait")
                self.assert_teardown_complete()
                self.assertTrue((self.R / "lease-closed.json").exists())

    def test_signal_during_teardown_is_ignored(self):
        self.tools.fail["1.5a"] = 1
        self.tools.signal_at["7.3"] = signal.SIGTERM
        self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        self.assert_teardown_complete()
        self.assertEqual(self.driver.signals, [signal.SIGTERM])
        self.assertIn("signals received", self.err.getvalue())

    def test_writer_unit_finishes_before_it_is_stopped(self):
        states = iter([b"active\n"] * 7 + [b"inactive\n"] * 100)
        self.tools.overrides["7.1-wait"] = lambda argv: (0, next(states))
        self.tools.signal_at["1.3d"] = signal.SIGTERM
        self.assertEqual(self.drive(), driver.EXIT_INTERRUPTED)
        labels = self.labels()
        self.assertGreater(labels[:labels.index("7.1")].count("7.1-wait"), 5)

    def test_a_writer_unit_that_never_ends_is_stopped_after_its_bound(self):
        self.tools.overrides["7.1-wait"] = lambda argv: (0, b"active\n")
        clock = {"now": FIXED}

        def tick():
            clock["now"] += 1
            return clock["now"]
        cfg = self.config()
        digest = self.plan(cfg)
        self.tools.calls.clear()
        self.tools.fail["1.5a"] = 1
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=tick, sleep=lambda _: None)
        self.assertEqual(d.run(digest), driver.EXIT_STEP_FAILED)
        self.assertIn("7.1", self.labels())
        self.assertIn("7.3", self.labels())


class RealChildren(Harness):
    """Steps handed to the REAL Runner, with bounds scaled down (the plan keeps the real ones)."""

    def scaled(self):
        return self.config(bound_scale=0.005)

    def test_a_step_child_past_its_bound_is_stopped(self):
        self.tools.real["1.5c"] = ["/bin/sh", "-c", "sleep 30"]
        started = time.monotonic()
        self.assertEqual(self.drive(self.scaled()), driver.EXIT_STEP_FAILED)
        self.assertLess(time.monotonic() - started, 20)
        receipt = json.loads((self.R / "driver/receipts/1.5c.json").read_text())
        self.assertEqual((receipt["stoppedBy"], receipt["childGone"]), ("bound", True))
        self.assert_teardown_complete()

    def test_cleanup_past_its_bound_escalates(self):
        self.tools.real["7.3"] = ["/bin/sh", "-c", "sleep 40"]
        self.tools.fail["1.5a"] = 1
        started = time.monotonic()
        self.assertEqual(self.drive(self.scaled()), driver.EXIT_ESCALATE)
        self.assertLess(time.monotonic() - started, 20)
        self.assertNotIn("7.6", self.labels())

    def test_host_steps_trapping_sigterm_report_through_two_signals(self):
        # The host steps get SIGTERM, take a moment for their clean-up and report 3; a second signal changes nothing.
        ready = self.tmp / "ready"
        self.tools.real["2.1-6"] = ["/bin/sh", "-c", f"trap 'sleep 1.5; exit 3' TERM; touch {ready}; sleep 60 & wait"]

        def signals():
            wait_for(ready)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.5)  # the child is now in its 1.5 s clean-up
            os.kill(os.getpid(), signal.SIGINT)
        timer = threading.Thread(target=signals)
        original = self.tools.run

        def run(argv, timeout, **kwargs):
            if kwargs["label"] == "2.1-6":
                timer.start()
            return original(argv, timeout, **kwargs)
        self.tools.run = run
        cfg = self.config(bound_scale=1.0)
        self.assertEqual(self.drive(cfg), driver.EXIT_ESCALATE)
        self.assertEqual(self.driver.host_exit, 3)
        self.assertEqual(self.driver.signals, [signal.SIGTERM, signal.SIGINT])
        self.assert_teardown_complete()

    def test_host_steps_that_ignore_sigterm_are_killed_and_escalate(self):
        self.tools.real["2.1-6"] = ["/bin/sh", "-c", "trap '' TERM; while :; do sleep 0.1; done"]
        self.tools.signal_at["1.7c"] = signal.SIGTERM  # interrupted already: the host step never starts
        self.assertEqual(self.drive(self.scaled()), driver.EXIT_INTERRUPTED)
        self.setUp()
        ready = self.tmp / "ready"
        self.tools.real["2.1-6"] = ["/bin/sh", "-c", f"trap '' TERM; touch {ready}; while :; do sleep 0.1; done"]
        timer = threading.Thread(target=lambda: (wait_for(ready), os.kill(os.getpid(), signal.SIGTERM)))
        original = self.tools.run

        def run(argv, timeout, **kwargs):
            if kwargs["label"] == "2.1-6":
                timer.start()
            return original(argv, timeout, **kwargs)
        self.tools.run = run
        self.assertEqual(self.drive(self.config(bound_scale=0.005)), driver.EXIT_ESCALATE)
        self.assertEqual(self.driver.host_exit, -signal.SIGKILL)


class TeardownFailure(Harness):
    def assert_left_armed(self):
        self.assertEqual(self.code, driver.EXIT_ESCALATE)
        self.assertNotIn("7.6", self.labels())  # never disarmed
        self.assertTrue((self.R / "provider-token").exists())  # kept for recovery
        self.assertTrue((self.R / "rehearsal_lease.py").exists() or "0.6b" not in self.labels())
        for name in SECRETS:
            self.assertFalse(os.path.lexists(self.R / name), name)  # not needed by recovery
        self.assertTrue((self.leases / (RUN + ".open")).exists())
        self.assertFalse((self.R / "lease-closed.json").exists())
        self.assertIn("ARMED (read active now)", self.err.getvalue())
        self.assertIn("resume-teardown", self.err.getvalue())
        self.assertIn("Kept on purpose", self.err.getvalue())

    def test_cleanup_failure_leaves_the_timer_armed(self):
        self.tools.fail["1.4"] = 1
        self.tools.fail["7.3"] = 1
        self.drive()
        self.assert_left_armed()
        self.assertNotIn("7.4", self.labels())

    def test_unresolved_dispatch_after_a_failed_create_escalates(self):
        self.tools.fail["1.3d"] = 1
        self.tools.fail["7.3"] = 1  # the real cleanup exits 1 while a dispatch is unresolved
        self.drive()
        self.assert_left_armed()
        self.assertIn("unresolved dispatch", self.err.getvalue())

    def test_inventory_mismatch_leaves_the_timer_armed(self):
        self.audit_baseline = dict(BASELINE, servers={})
        self.drive()
        self.assert_left_armed()
        self.assertIn("7.4", self.labels())
        self.assertNotIn("7.5-show", self.labels())  # stops at the inventory, before the audit
        self.assertIn("independent inventory does not equal the baseline", self.err.getvalue())

    def test_final_audit_failure_leaves_the_timer_armed(self):
        self.absence_reads = 2
        self.drive()
        self.assert_left_armed()
        self.assertFalse(json.loads(next((self.R / "driver").glob("final-audit.json")).read_text())["absence"])

    def test_final_audit_reads_the_writer_units(self):
        self.tools.overrides["7.5-show"] = lambda argv: (0, b"active\n")
        self.drive()
        self.assert_left_armed()

    def test_disarm_failure_escalates_with_the_timer_state_unknown(self):
        self.tools.fail["7.6"] = 1
        self.drive()
        self.assertEqual(self.code, driver.EXIT_ESCALATE)
        self.assertTrue((self.R / "provider-token").exists())
        self.assertIn("NOT verified: disarm failed", self.err.getvalue())

    def test_timer_not_read_inactive_after_disarm_escalates(self):
        self.tools.overrides["7.6-show"] = lambda argv: (1, b"")
        self.drive()
        self.assertEqual(self.code, driver.EXIT_ESCALATE)
        self.assertIn("could not be read inactive", self.err.getvalue())

    def test_unit_read_failure_or_empty_output_is_unknown(self):
        for answer in ((1, b"inactive\n"), (0, b""), (0, b"activating\n")):
            with self.subTest(answer=answer):
                self.setUp()
                self.tools.overrides["7.1-show"] = lambda argv, answer=answer: answer
                self.drive()
                self.assertEqual(self.code, driver.EXIT_ESCALATE)
                self.assertNotIn("7.3", self.labels())

    def test_receipt_write_failure_does_not_skip_cleanup(self):
        original = lease.write_private

        def failing(path, value):
            if "receipts" in str(path):
                raise OSError(28, "No space left on device")
            return original(path, value)
        lease.write_private = failing
        try:
            self.tools.fail["1.5a"] = 1
            self.assertEqual(self.drive(), driver.EXIT_STEP_FAILED)
        finally:
            lease.write_private = original
        self.assert_teardown_complete()

    def test_closing_write_failure_still_escalates_with_recovery_text(self):
        original = lease.write_private

        def failing(path, value):
            if Path(path).name.startswith(("closing", "lease-closed")):
                raise OSError(28, "No space left on device")
            return original(path, value)
        lease.write_private = failing
        try:
            self.tools.fail["1.4"] = 1
            self.tools.fail["7.3"] = 1
            self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        finally:
            lease.write_private = original
        self.assertIn("ESCALATE", self.err.getvalue())

    def test_abort_or_refresher_errors_do_not_skip_teardown(self):
        for name in ("abort_current", "stop_refresher"):
            with self.subTest(name=name):
                self.setUp()
                self.tools.fail["1.5a"] = 1
                target = self.tools if name == "abort_current" else driver.Driver
                original = getattr(target, name)

                def broken(*args, original=original, name=name):
                    if name == "abort_current" or args[0].teardown_running:
                        raise PermissionError(1, "Operation not permitted")
                    return original(*args)
                setattr(target, name, broken)
                try:
                    self.drive()
                finally:
                    setattr(target, name, original)
                self.assertIn("7.3", self.labels())
                self.assertIn("7.6", self.labels())

    def test_deletion_continues_after_a_failure(self):
        original = driver.remove_inside

        def flaky(run, relative):
            if relative == "deploy-key.sec":
                raise driver.Failed("read-only")
            return original(run, relative)
        driver.remove_inside = flaky
        try:
            self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        finally:
            driver.remove_inside = original
        self.assertTrue((self.R / "deploy-key.sec").exists())
        for name in ("provider-token", "spare-key.sec", "escrow.age", "secrets"):
            self.assertFalse(os.path.lexists(self.R / name), name)
        self.assertIn("deploy-key.sec", self.err.getvalue())

    def test_main_net_falls_back_to_the_fixed_text(self):
        args = ["run", "--run-directory", str(self.R), "--revision", REV, "--source-manifest-sha256", "0" * 64,
                "--kexec-archive", "/x", "--kexec-archive-sha256", "0" * 64, "--ssh-cidr", "198.51.100.9/32",
                "--unit-path", "/x", "--expect-baseline", "servers=1", "--ssh-keygen", "/x", "--nix", "/x",
                "--nix-store", "/x", "--rclone", "/x", "--restic", "/x", "--sops", "/x", "--terrace-secrets", "/x",
                "--approved-plan-sha", "0" * 64]
        originals = driver.Driver.run, driver.recovery_text
        driver.Driver.run = lambda self, sha: (_ for _ in ()).throw(RuntimeError("boom"))
        driver.recovery_text = lambda cfg, summary: (_ for _ in ()).throw(KeyError("x"))
        try:
            lease.write_private(self.R / "lease.json", {"schema": 1})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(driver.main(args, runner=self.tools), driver.EXIT_ESCALATE)
            self.assertIn("full recovery text could not be built", err.getvalue())
        finally:
            driver.Driver.run, driver.recovery_text = originals

    def test_main_maps_an_error_after_the_start_to_escalate(self):
        args = ["run", "--run-directory", str(self.R), "--revision", REV, "--source-manifest-sha256", "0" * 64,
                "--kexec-archive", "/x", "--kexec-archive-sha256", "0" * 64, "--ssh-cidr", "198.51.100.9/32",
                "--unit-path", "/x", "--expect-baseline", "servers=1", "--ssh-keygen", "/x", "--nix", "/x",
                "--nix-store", "/x", "--rclone", "/x", "--restic", "/x", "--sops", "/x", "--terrace-secrets", "/x",
                "--approved-plan-sha", "0" * 64]
        original = driver.Driver.run
        driver.Driver.run = lambda self, sha: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(driver.main(args, runner=self.tools), driver.EXIT_INPUTS)
            lease.write_private(self.R / "lease.json", {"schema": 1})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(driver.main(args, runner=self.tools), driver.EXIT_ESCALATE)
            self.assertIn("ESCALATE", err.getvalue())
        finally:
            driver.Driver.run = original

    def test_resume_teardown_after_escalation(self):
        self.tools.fail["1.4"] = 1
        self.tools.fail["7.3"] = 1
        self.drive()
        del self.tools.fail["7.3"]
        self.tools.calls.clear()
        cfg = self.config()
        code = driver.resume_teardown(cfg, self.tools, out=self.out, err=self.err, sleep=lambda _: None)
        self.assertEqual(code, driver.EXIT_PASS, self.err.getvalue())
        self.assertEqual(self.labels(), TEARDOWN_LABELS)
        self.assertFalse((self.R / "provider-token").exists())
        self.assertTrue((self.R / "lease-closed.json").exists())
        self.assertFalse((self.leases / (RUN + ".open")).exists())

    def test_resume_delete_after_person_check(self):
        self.tools.fail["1.3d"] = 1
        self.tools.fail["7.3"] = 1
        self.sftp_left_running = True
        (self.R / "sftp/sftp-listener.json").write_text("{}")
        self.drive()
        cfg = self.config()
        self.tools.overrides["resume-load"] = lambda argv: (0, b"loaded\n")
        self.assertEqual(driver.resume_teardown(cfg, self.tools, delete_after_person_check=True, out=self.out,
                                                err=self.err), driver.EXIT_ESCALATE)
        self.assertTrue((self.R / "provider-token").exists())
        self.tools.overrides["resume-load"] = lambda argv: (0, b"not-found\n")
        self.audit_baseline = dict(BASELINE, servers={})
        self.assertEqual(driver.resume_teardown(cfg, self.tools, delete_after_person_check=True, out=self.out,
                                                err=self.err), driver.EXIT_ESCALATE)
        self.assertTrue((self.R / "provider-token").exists())
        self.audit_baseline = BASELINE
        self.tools.calls.clear()
        self.assertEqual(driver.resume_teardown(cfg, self.tools, delete_after_person_check=True, out=self.out,
                                                err=self.err), driver.EXIT_PASS)
        self.assertIn("7.2", self.labels())
        self.assertNotIn("7.6", self.labels())
        self.assertFalse((self.R / "provider-token").exists())
        self.assertFalse((self.R / "sftp").exists())
        self.assertTrue(list(self.R.glob("lease-closed*.json")))
        self.assertFalse((self.leases / (RUN + ".open")).exists())


class Deletion(Harness):
    def test_nothing_outside_the_run_directory_is_deleted(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        sibling = self.leases / ("d" * 32)
        sibling.mkdir(mode=0o700)
        (sibling / "provider-token").write_text("other run")
        (sibling / "lease-closed.json").write_text("{}")
        (self.leases / "notes").write_text("keep")
        (self.R / "secrets/escape").symlink_to(outside / "keep")
        (self.R / "sftp/escape-dir").symlink_to(outside)
        (self.R / "host-stage/escape").symlink_to(outside)
        self.assertEqual(self.drive(), driver.EXIT_PASS)
        self.assertEqual((outside / "keep").read_text(), "keep")
        self.assertEqual((sibling / "provider-token").read_text(), "other run")
        self.assertTrue((self.leases / "notes").exists())
        self.assertTrue(self.source.joinpath("infra/cloud/rehearsal_lease.py").exists())

    def test_remove_inside_refuses_paths_out_of_the_run_directory(self):
        for bad in ("../x", "/etc/passwd", "a/../../x"):
            with self.subTest(bad=bad), self.assertRaises(driver.Failed):
                driver.remove_inside(self.R, bad)
        (self.tmp / "target").mkdir()
        (self.tmp / "target/file").write_text("x")
        (self.R / "foundation").symlink_to(self.tmp / "target")
        with self.assertRaises(driver.Failed):
            driver.remove_inside(self.R, "foundation/file")
        self.assertTrue((self.tmp / "target/file").exists())


class Refusals(Harness):
    def refused(self, pattern, cfg=None, start=FIXED):
        with self.assertRaisesRegex(driver.Refused, pattern):
            driver.make_plan(cfg or self.config(), self.tools, start)

    def at(self, text):
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()

    def test_window_boundaries(self):
        for start in ("2026-12-01 03:45:00", "2026-12-01 17:55:00", "2026-12-01 12:00:00"):
            with self.subTest(start=start):
                driver.make_plan(self.config(), self.tools, self.at(start))
        for start in ("2026-12-01 03:44:59", "2026-12-01 17:55:01", "2026-12-01 23:00:00", "2026-12-01 02:00:00",
                      "2026-12-31 22:30:00", "2026-12-01 03:12:00", "2026-11-30 20:00:00"):
            with self.subTest(start=start):
                self.refused("G7", start=self.at(start))

    def test_window_arithmetic(self):
        def span(a, b):
            return driver.window_conflicts(datetime.fromisoformat(a + "+00:00"), datetime.fromisoformat(b + "+00:00"))
        self.assertEqual(span("2026-12-01T03:45:00", "2026-12-01T22:25:00"), [])
        self.assertEqual(len(span("2026-12-01T03:44:59", "2026-12-01T04:00:00")), 1)
        self.assertEqual(len(span("2026-12-01T22:24:00", "2026-12-01T22:26:00")), 1)
        self.assertEqual(len(span("2026-12-31T23:59:00", "2027-01-01T00:01:00")), 1)  # across midnight and the year
        self.assertEqual(len(span("2026-12-01T03:09:00", "2026-12-01T03:20:00")), 2)  # both spans
        self.assertEqual(span("2026-12-01T03:10:00", "2026-12-01T03:15:00"), [])  # the gap between them

    def arm_at(self, text):
        self.setUp()
        cfg = self.config()
        digest = self.plan(cfg)
        self.tools.calls.clear()
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: self.at(text), sleep=lambda _: None)
        d.start = lambda approved: driver.make_plan(cfg, self.tools, FIXED)  # approved earlier, started late
        return d.run(digest)

    def test_window_rechecked_at_arming_including_the_timer_cleanup_tail(self):
        # Lease plus the timer's own cleanup: 15300 s. 18:10:00 + 15300 s = 22:25:00 (touches the block: allowed).
        self.assertEqual(self.arm_at("2026-12-01 18:10:00"), driver.EXIT_PASS)
        self.assertEqual(self.arm_at("2026-12-01 18:10:01"), driver.EXIT_STEP_FAILED)
        self.assertNotIn("1.1a", self.labels())
        self.assertNotIn("7.3", self.labels())

    def test_kexec_archive_refusals(self):
        diagnostic = self.store / f"{STORE_HASH}-terrace-kexec-tarball-diagnostic"
        diagnostic.mkdir()
        (diagnostic / "nixos-kexec-installer-x86_64-linux.tar.gz").write_bytes(b"x")
        self.refused("G5", self.config(kexec_archive=diagnostic / "nixos-kexec-installer-x86_64-linux.tar.gz"))
        renamed = self.tmp / "stock-kexec-network-diagnostic.tar.gz"
        renamed.symlink_to(self.archive)  # the genuine bytes and store path, under a diagnostic name
        self.refused("G5: kexec archive refused", self.config(kexec_archive=renamed))
        other = self.store / f"{'z' * 32}-terrace-kexec-tarball"
        other.mkdir()
        (other / "nixos-kexec-installer-x86_64-linux.tar.gz").write_bytes(b"kexec archive bytes")
        self.refused("G5: the kexec archive is not", self.config(kexec_archive=other / "nixos-kexec-installer-x86_64-linux.tar.gz"))
        self.refused("G5: kexec archive digest", self.config(kexec_sha256="0" * 64))
        self.tools.fail["plan-kexec-eval"] = 1
        self.refused("G5: could not evaluate")

    def test_store_path_refusals(self):
        self.tools.fail["plan-store-validity"] = 1
        self.refused("G6: a prestaged store path is missing")
        del self.tools.fail["plan-store-validity"]
        self.tools.overrides["plan-store-roots"] = lambda argv: (0, b"/elsewhere -> x\n")
        self.refused("G6: lease is not a GC root")
        del self.tools.overrides["plan-store-roots"]
        bad = self.store / f"{'m' * 32}-lease-variants.json"
        bad.write_text(json.dumps(dict(VARIANTS, v0=VARIANTS["v1"])))
        self.refused("G6: variants", self.config(variants=bad))

    def test_open_lease_refusals(self):
        (self.leases / ("e" * 32 + ".open")).write_text("x")
        self.refused("another lease is open")
        (self.leases / ("e" * 32 + ".open")).unlink()
        other = self.leases / ("f" * 32)
        other.mkdir(mode=0o700)
        (other / "lease.json").write_text("{}")
        self.refused("not closed")
        (other / "lease-closed.json").write_text("{}")
        driver.make_plan(self.config(), self.tools, FIXED)
        (self.leases / (RUN + ".open")).write_text("x")
        self.refused("own open pointer")
        (self.leases / (RUN + ".open")).unlink()
        lease.write_private(self.R / "lease.json", {})
        self.refused("already exists")

    def test_other_input_refusals(self):
        outside = self.tmp / "token"
        outside.write_text(PLANTED["token"])
        outside.chmod(0o600)
        self.refused("credential file must be a 0600 copy directly inside", self.config(credential_file=outside))
        (self.R / "provider-token").chmod(0o644)
        self.refused("credential")
        (self.R / "provider-token").chmod(0o600)
        self.refused("manifest", self.config(manifest_sha256="0" * 64))
        self.refused("ssh-cidr", self.config(ssh_cidr="0.0.0.0/0"))
        self.refused("free", self.config(min_free_bytes=1 << 62))
        self.refused("absolute path", self.config(nix="nix"))
        self.refused("child-path", self.config(child_path="relative/bin"))
        self.refused("unit-path", self.config(unit_path=str(self.tmp / "no-such-dir")))
        self.tools.overrides["idle-units"] = lambda argv: (0, b"terrace-cloud-x.timer loaded active waiting\n")
        self.refused("not idle")
        del self.tools.overrides["idle-units"]
        (self.source / "infra/nixos/lease_host_steps.py").unlink()
        self.refused("lease_host_steps.py", self.config(manifest_sha256=driver.tree_manifest(self.source)))

    def test_refuses_root(self):
        driver.running_as_root = lambda: True
        self.refused("root")
        self.assertEqual(driver.resume_teardown(self.config(), self.tools, out=self.out, err=self.err),
                         driver.EXIT_INPUTS)

    def test_unexpected_input_error_exits_2_with_nothing_started(self):
        cfg = self.config()
        digest = self.plan(cfg)
        (self.R / "lease-inputs.json").write_text("{not json")
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: FIXED)
        self.assertEqual(d.run(digest), driver.EXIT_INPUTS)
        self.assertFalse((self.leases / (RUN + ".open")).exists())
        self.assertFalse((self.R / "driver").exists())

    def test_queue_check_is_reported_whether_or_not_supplied(self):
        plan = driver.make_plan(self.config(), self.tools, FIXED)
        queue = [c for c in plan["checks"] if c["check"] == "heavy queue"][0]
        self.assertEqual(queue["result"], "NOT MADE")
        self.assertIn("not made", driver.render_plan(plan, "0" * 64))
        q = str(self.bin / "queue-idle")
        plan = driver.make_plan(self.config(queue_command=q), self.tools, FIXED)
        self.assertEqual([c for c in plan["checks"] if c["check"] == "heavy queue"][0]["detail"],
                         f"supplied: {q} exit 0")
        self.tools.fail["idle-queue"] = 1
        self.refused("heavy-queue check", self.config(queue_command=q))

    def test_run_refuses_an_unapproved_or_changed_plan(self):
        cfg = self.config()
        digest = self.plan(cfg)
        d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: FIXED)
        self.assertEqual(d.run("0" * 64), driver.EXIT_INPUTS)
        changed = self.config(ssh_cidr="198.51.100.10/32")
        d = driver.Driver(changed, self.tools, out=self.out, err=self.err, now=lambda: FIXED)
        self.assertEqual(d.run(digest), driver.EXIT_INPUTS)
        self.assertFalse((self.leases / (RUN + ".open")).exists())

    def changed_between_plan_and_run(self, change):
        cfg = self.config()
        digest = self.plan(cfg)
        undo = change()
        try:
            d = driver.Driver(cfg, self.tools, out=self.out, err=self.err, now=lambda: FIXED)
            return d.run(digest)
        finally:
            undo()

    def test_plan_digest_binds_steps_teardown_tools_and_host_steps(self):
        def steps():
            original = driver.run_steps

            def changed(cfg):
                result = original(cfg)
                result[0].bound += 1
                return result
            driver.run_steps = changed
            return lambda: setattr(driver, "run_steps", original)

        def teardown():
            original = driver.TEARDOWN
            driver.TEARDOWN = original[:-1]
            return lambda: setattr(driver, "TEARDOWN", original)

        def tool():
            (self.bin / "restic").write_text("#!/bin/sh\nexit 1\n")
            return lambda: None

        def host_list():
            self.tools.overrides["plan-host-steps-list"] = lambda argv: (0, HOST_LIST + b"7  something new\n")
            return lambda: self.tools.overrides.pop("plan-host-steps-list")
        for name, change in (("steps", steps), ("teardown", teardown), ("tool", tool), ("host steps", host_list)):
            with self.subTest(name=name):
                self.setUp()
                self.assertEqual(self.changed_between_plan_and_run(change), driver.EXIT_INPUTS)
                self.assertIn("changed since the plan", self.err.getvalue())

    def test_plan_lists_every_step_with_its_fields(self):
        plan = driver.make_plan(self.config(), self.tools, FIXED)
        self.assertEqual([s["id"] for s in plan["steps"]], [s.id for s in driver.run_steps(self.config())])
        for step in plan["steps"]:
            self.assertTrue(all(step[key] for key in ("command", "guard", "expected", "evidence", "stop")), step)
        text = driver.render_plan(plan, "f" * 64)
        for needle in ("cap 5.0 USD", "expiry", "--approved-plan-sha " + "f" * 64, "systemd-run",
                       "RuntimeMaxSec=230", "--server-type cpx42 --location nbg1", "servers=1, primary_ips=2, ssh_keys=1",
                       SYSTEM, DISKO, INSTALLER, VARIANTS["v5"], "throwaway SFTP endpoint and reverse forward",
                       "Runs as root", "Exit codes", "sha256"):
            self.assertIn(needle, text)
        self.assertNotIn(PLANTED["token"], text)


class Units(Harness):
    def test_bounded_units_match_what_the_tools_require(self):
        cfg = self.config()
        for step in [s for s in driver.run_steps(cfg) if s.unit]:
            argv = driver.bounded_unit(cfg, step.unit, list(step.command))
            tool = Path(step.command[4]).name
            if tool == "rehearsal_provider_actions.py":
                scope, kind = f"{step.command[6]}-{step.command[7]}", "create"
            else:
                scope, kind = step.command[6], step.command[5]
            seen = {}

            class FakePath:
                def __init__(self, _path):
                    pass

                def read_text(self):
                    return "0::/system.slice/" + step.unit + "\n"
            properties = {"--property=KillMode=control-group": ("KillMode", "control-group"),
                          "--service-type=exec": ("Type", "exec"),
                          "--property=RuntimeMaxSec=230": ("RuntimeMaxUSec", "3min 50s"),
                          "--property=TimeoutStopSec=10s": ("TimeoutStopUSec", "10s")}
            granted = dict(properties[a] for a in argv if a in properties)
            original_path, original_read = apply.Path, apply.timer.read_properties
            apply.Path = FakePath
            apply.timer.read_properties = lambda unit, expected: seen.update(unit=unit) or {
                key: (str(os.getpid()) if key == "MainPID" else granted.get(key)) for key in expected}
            try:
                apply.require_run_unit(RUN, scope, kind)
            finally:
                apply.Path, apply.timer.read_properties = original_path, original_read
            self.assertEqual(seen["unit"], step.unit, step.id)


class Refresher(Harness):
    def test_binding_refresh_failure_stops_the_running_step(self):
        cfg = self.config(refresh_seconds=0.05)
        self.tools.hang.add("1.6b")
        self.tools.fail["1.4-refresh"] = 1
        self.assertEqual(self.drive(cfg), driver.EXIT_STEP_FAILED)
        # The failed refresh stops whichever binding consumer is running when it lands (1.5a to 1.6b).
        failed = [json.loads(path.read_text()) for path in (self.R / "driver/receipts").glob("1.[56]*.json")]
        failed = [r for r in failed if r["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("binding refresh failed", failed[0]["error"])
        self.assertNotIn("1.7a", self.labels())
        self.assertIn("7.3", self.labels())

    def test_a_refresher_that_raises_is_a_failed_refresh(self):
        cfg = self.config(refresh_seconds=0.05)
        self.tools.hang.add("1.6b")
        original = driver.Driver.run_child

        def raising(this, argv, bound, *, label, **kwargs):
            if label == "1.4-refresh":
                raise OSError("cannot start the binding child")
            return original(this, argv, bound, label=label, **kwargs)
        driver.Driver.run_child = raising
        try:
            self.assertEqual(self.drive(cfg), driver.EXIT_STEP_FAILED)
        finally:
            driver.Driver.run_child = original
        failed = [json.loads(path.read_text()) for path in (self.R / "driver/receipts").glob("1.[56]*.json")]
        self.assertIn("binding refresh failed", [r for r in failed if r["status"] == "failed"][0]["error"])
        self.assertNotIn("1.7a", self.labels())

    def test_refresher_runs_only_around_the_binding_consumers(self):
        cfg = self.config(refresh_seconds=0.01)
        original = self.tools.effect

        def slow(label, argv):
            if label in ("1.5a", "1.6b", "1.7a", "2.1-6"):
                time.sleep(0.1)
            return original(label, argv)
        self.tools.effect = slow
        self.assertEqual(self.drive(cfg), driver.EXIT_PASS)
        labels = self.labels()
        refreshes = [i for i, label in enumerate(labels) if label == "1.4-refresh"]
        self.assertTrue(refreshes)
        self.assertGreater(min(refreshes), labels.index("1.4"))
        self.assertLess(max(refreshes), labels.index("1.7a"))


class Verification(Harness):
    """Children that cannot be verified gone, teardown children that raise, and the last-resort nets."""

    def test_teardown_child_not_verified_gone_escalates_and_never_disarms(self):
        self.tools.overrides["7.3"] = lambda argv: driver.Outcome(0, b"", None, False)
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertNotIn("7.4", self.labels())
        self.assertNotIn("7.6", self.labels())
        self.assertIn("[7.3] FAILED", self.out.getvalue())

    def test_stop_child_not_verified_gone_escalates_although_the_rest_verifies(self):
        self.tools.overrides["7.1"] = lambda argv: driver.Outcome(0, b"", None, False)
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertIn("7.6", self.labels())
        self.assertIn("7.1: its child could not be verified gone", self.err.getvalue())

    def test_host_steps_child_not_verified_gone_escalates(self):
        def host(argv):
            self.tools.effect("2.1-6", argv)
            return driver.Outcome(0, b"", None, False)
        self.tools.overrides["2.1-6"] = host
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertFalse(self.driver.host_verified)

    def test_leftover_child_not_settled_escalates(self):
        self.tools.settle = lambda seconds: False
        self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        self.assertIn("not verified gone", self.err.getvalue())

    def test_a_teardown_child_that_raises_is_never_read_as_success(self):
        for label, absent in (("7.3", "7.4"), ("7.4", "7.5-show"), ("7.6", "7.6-show")):
            with self.subTest(label=label):
                self.setUp()
                original = driver.Driver.run_child

                def raising(this, argv, bound, *, label, original=original, target=label, **kwargs):
                    if label == target:
                        raise RuntimeError("child could not be started")
                    return original(this, argv, bound, label=label, **kwargs)
                driver.Driver.run_child = raising
                try:
                    self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
                finally:
                    driver.Driver.run_child = original
                self.assertNotIn(absent, self.labels())
                self.assertNotEqual(self.driver.state["timer"], "disarmed")
                self.assertTrue((self.R / "provider-token").exists())

    def test_teardown_that_raises_still_escalates(self):
        original = driver.Driver.teardown
        driver.Driver.teardown = lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        finally:
            driver.Driver.teardown = original
        self.assertIn("teardown raised unexpectedly", self.err.getvalue())

    def test_recovery_text_that_raises_falls_back_to_the_fixed_text(self):
        original = driver.recovery_text
        driver.recovery_text = lambda cfg, summary: (_ for _ in ()).throw(KeyError("x"))
        try:
            self.tools.fail["1.4"] = 1
            self.tools.fail["7.3"] = 1
            self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
        finally:
            driver.recovery_text = original
        self.assertIn("full recovery text could not be built", self.err.getvalue())

    def test_every_child_runs_in_the_run_directory(self):
        self.drive()
        self.assertEqual(set(self.tools.cwds), {str(self.R)})

    def test_timer_state_is_reported_as_read(self):
        for answer, text in (((0, b"active\n"), "ARMED (read active now)"),
                             ((0, b"inactive\n"), "NOT armed (read inactive now)"),
                             ((1, b""), "UNKNOWN (its state could not be read)")):
            with self.subTest(answer=answer):
                self.setUp()
                self.lease_baseline = dict(BASELINE, ssh_keys={})  # fails at 1.1a, before arming
                # The service answers differently, so reading the wrong unit fails.
                other = (0, b"inactive\n") if answer == (0, b"active\n") else (0, b"active\n")
                self.tools.overrides["7.7-timer"] = (
                    lambda argv, answer=answer, other=other: answer if argv[2].endswith(".timer") else other)
                self.assertEqual(self.drive(), driver.EXIT_ESCALATE)
                self.assertNotIn("1.1b", self.labels())
                self.assertIn(text, self.err.getvalue())

    def test_resume_error_after_a_lease_exists_is_escalate(self):
        original = driver.resume_teardown
        driver.resume_teardown = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            lease.write_private(self.R / "lease.json", {"schema": 1})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(driver.main(["resume-teardown", "--run-directory", str(self.R)], runner=self.tools),
                                 driver.EXIT_ESCALATE)
            self.assertIn("ESCALATE", err.getvalue())
        finally:
            driver.resume_teardown = original

    def test_person_check_refuses_while_a_writer_unit_is_active(self):
        self.tools.fail["1.3d"] = 1
        self.tools.fail["7.3"] = 1
        self.drive()
        self.tools.overrides["resume-units"] = lambda argv: (0, b"active\n")
        self.tools.calls.clear()
        self.assertEqual(driver.resume_teardown(self.config(), self.tools, delete_after_person_check=True,
                                                out=self.out, err=self.err), driver.EXIT_ESCALATE)
        self.assertNotIn("7.4", self.labels())
        self.assertTrue((self.R / "provider-token").exists())

    def test_plan_records_the_real_bounds(self):
        cfg = self.config(bound_scale=0.01)
        plan = driver.make_plan(cfg, self.tools, FIXED)
        self.assertEqual([s["boundSeconds"] for s in plan["steps"]], [s.bound for s in driver.run_steps(cfg)])
        self.assertIn(7200, [s["boundSeconds"] for s in plan["steps"]])

    def test_sudo_and_path_directories_are_bound_into_the_digest(self):
        def changed_sudo():
            (self.sbin / "sudo").write_text("#!/bin/sh\nexit 7\n")
            return lambda: None

        def other_sudo_path():
            other = self.tmp / "other-sudo"
            shutil.copy(self.sbin / "sudo", other)
            original = self.config
            self.config = lambda **changes: original(**{"sudo": str(other), **changes})
            return lambda: setattr(self, "config", original)

        def new_binary_in_child_path():
            (self.bin / "extra").write_text("#!/bin/sh\n")
            return lambda: None

        def new_binary_in_unit_path():
            (self.tmp / "node-bin" / "node").write_text("#!/bin/sh\n")
            return lambda: None
        for name, change in (("sudo contents", changed_sudo), ("sudo path", other_sudo_path),
                             ("child path", new_binary_in_child_path), ("unit path", new_binary_in_unit_path)):
            with self.subTest(name=name):
                self.setUp()
                cfg = self.config()
                digest = self.plan(cfg)
                undo = change()
                try:
                    d = driver.Driver(self.config(), self.tools, out=self.out, err=self.err, now=lambda: FIXED)
                    self.assertEqual(d.run(digest), driver.EXIT_INPUTS)
                finally:
                    undo()
                self.assertIn("changed since the plan", self.err.getvalue())


class Draining(unittest.TestCase):
    def test_a_child_printing_on_sigterm_is_drained_and_reports_its_own_status(self):
        ready = Path(tempfile.mkdtemp()) / "ready"
        script = f"trap 'head -c 300000 /dev/zero; exit 0' TERM; touch {ready}; sleep 60 & wait"
        started = time.monotonic()
        outcome = driver.Runner().run(["/bin/sh", "-c", script], 60, label="t", grace=12,
                                      interrupted=lambda: ready.exists())
        self.assertEqual((outcome.code, outcome.reason, outcome.gone), (0, "interrupted", True))
        self.assertLess(time.monotonic() - started, 8)


class ScriptedGroup:
    """A fake killpg: scripted answers for the probe (signal 0) and for each real signal, on a fake clock."""

    def __init__(self, probe, on_signal=None):
        self.now = 0.0
        self.probe = probe            # callable(now, signals_sent) -> None (alive), or an exception to raise
        self.on_signal = on_signal or (lambda sig: None)
        self.sent = []                # (time, signal)

    def killpg(self, pgid, sig):
        if sig == 0:
            answer = self.probe(self.now, [s for _, s in self.sent])
            if answer is not None:
                raise answer
            return
        self.sent.append((round(self.now, 2), sig))
        answer = self.on_signal(sig)
        if answer is not None:
            raise answer

    def runner(self):
        return driver.Runner(killpg=self.killpg, monotonic=lambda: self.now, sleep=self.advance)

    def advance(self, seconds):
        self.now += seconds


class ExitedChild:
    pid = 4242
    returncode = 0

    def poll(self):
        return 0


EPERM = PermissionError(1, "Operation not permitted")
ESRCH = ProcessLookupError(3, "No such process")


class GroupStop(unittest.TestCase):
    """Runner.stop after the child has exited: probe 1 s, then SIGTERM and 3 s, then SIGKILL and 5 s."""

    def stop(self, group):
        return group.runner().stop(ExitedChild(), 1)

    def test_group_alive_answers(self):
        for answer, expected in ((None, True), (ESRCH, False), (EPERM, None)):
            group = ScriptedGroup(lambda now, sent, answer=answer: answer)
            self.assertIs(group.runner().group_alive(1), expected)

    def test_empty_group_is_gone_without_a_signal(self):
        group = ScriptedGroup(lambda now, sent: ESRCH)
        self.assertTrue(self.stop(group))
        self.assertEqual(group.sent, [])

    def test_permission_denied_probe_is_never_gone_and_the_bound_returns_not_gone(self):
        group = ScriptedGroup(lambda now, sent: EPERM, on_signal=lambda sig: EPERM)
        self.assertFalse(self.stop(group))
        self.assertEqual([sig for _, sig in group.sent], [signal.SIGTERM, signal.SIGKILL])
        self.assertAlmostEqual(group.sent[0][0], 1.0, delta=0.06)   # the 1 s observation stage first
        self.assertAlmostEqual(group.sent[1][0], 4.0, delta=0.11)   # then SIGTERM and 3 s
        self.assertAlmostEqual(group.now, 9.0, delta=0.2)           # then SIGKILL and 5 s, then not gone

    def test_a_live_group_that_never_empties_is_not_gone(self):
        group = ScriptedGroup(lambda now, sent: None)
        self.assertFalse(self.stop(group))
        self.assertEqual([sig for _, sig in group.sent], [signal.SIGTERM, signal.SIGKILL])

    def test_a_group_that_empties_during_observation_gets_no_signal(self):
        group = ScriptedGroup(lambda now, sent: ESRCH if now >= 0.5 else None)
        self.assertTrue(self.stop(group))
        self.assertEqual(group.sent, [])

    def test_a_group_that_empties_after_sigterm(self):
        group = ScriptedGroup(lambda now, sent: ESRCH if signal.SIGTERM in sent and now >= 2 else None)
        self.assertTrue(self.stop(group))
        self.assertEqual([sig for _, sig in group.sent], [signal.SIGTERM])

    def test_a_group_that_empties_only_after_sigkill(self):
        group = ScriptedGroup(lambda now, sent: ESRCH if signal.SIGKILL in sent and now >= 6 else None)
        self.assertTrue(self.stop(group))
        self.assertEqual([sig for _, sig in group.sent], [signal.SIGTERM, signal.SIGKILL])

    def test_permission_denied_that_clears_to_empty_is_gone(self):
        group = ScriptedGroup(lambda now, sent: EPERM if now < 0.5 else ESRCH)
        self.assertTrue(self.stop(group))

    def test_permission_denied_then_live_is_not_gone(self):
        group = ScriptedGroup(lambda now, sent: EPERM if now < 0.5 else None)
        self.assertFalse(self.stop(group))

    def test_no_such_group_at_the_signal_step_means_gone(self):
        group = ScriptedGroup(lambda now, sent: None, on_signal=lambda sig: ESRCH)
        self.assertTrue(self.stop(group))
        self.assertEqual([sig for _, sig in group.sent], [signal.SIGTERM])


class DirectoryIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store"
        self.store.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("a", "b"):
            (self.bin / name).write_text("12345")
            os.utime(self.bin / name, ns=(10 ** 18, 10 ** 18))
        (self.bin / "tool").symlink_to(self.bin / "a")

    def identity(self):
        return driver.directory_identity(str(self.bin), str(self.store))

    def assert_changes(self, change):
        before = self.identity()
        change()
        self.assertNotEqual(self.identity(), before)

    def test_unchanged_directory_is_stable(self):
        self.assertEqual(self.identity(), self.identity())

    def test_a_re_pointed_link_changes_it(self):
        def repoint():  # same name, same size, same mtime: only the target differs
            (self.bin / "tool").unlink()
            (self.bin / "tool").symlink_to(self.bin / "b")
        self.assert_changes(repoint)

    def test_a_size_change_changes_it(self):
        def grow():
            (self.bin / "a").write_text("123456")
            os.utime(self.bin / "a", ns=(10 ** 18, 10 ** 18))
        self.assert_changes(grow)

    def test_an_mtime_change_changes_it(self):
        self.assert_changes(lambda: os.utime(self.bin / "a", ns=(10 ** 18, 10 ** 18 + 1)))

    def test_a_rename_changes_it(self):
        def rename():  # the link keeps its target, size and mtime; only its name differs
            (self.bin / "tool").rename(self.bin / "tool2")
        self.assert_changes(rename)

    def test_a_store_directory_is_its_real_path(self):
        first, second = self.store / ("a" * 32 + "-path"), self.store / ("b" * 32 + "-path")
        for directory in (first, second):
            (directory / "bin").mkdir(parents=True)
        link = self.tmp / "profile"
        link.symlink_to(first / "bin")
        before = driver.directory_identity(str(link), str(self.store))
        self.assertEqual(before, {"real": os.path.realpath(first / "bin")})
        link.unlink()
        link.symlink_to(second / "bin")  # a profile switch
        self.assertEqual(driver.directory_identity(str(link), str(self.store)),
                         {"real": os.path.realpath(second / "bin")})


class LiveChild:
    """A child that never ends: poll() stays None whatever is sent."""
    pid = 4343
    returncode = None

    def poll(self):
        return None


class ChildThatSurvives(unittest.TestCase):
    def test_a_child_alive_after_sigterm_and_sigkill_is_not_gone(self):
        group = ScriptedGroup(lambda now, sent: None)
        self.assertFalse(group.runner().stop(LiveChild(), 30))
        self.assertEqual(group.sent, [(0.0, signal.SIGTERM), (30.0, signal.SIGKILL)])
        self.assertAlmostEqual(group.now, 40.0, delta=0.2)

    def test_through_run_the_surviving_child_is_a_leftover(self):
        started = time.monotonic()

        def killpg(pgid, sig):
            if sig == signal.SIGKILL:
                return  # the kill never lands, as for a child in uninterruptible sleep
            os.killpg(pgid, sig)
        runner = driver.Runner(killpg=killpg, monotonic=lambda: (time.monotonic() - started) * 50)
        ready = Path(tempfile.mkdtemp()) / "ready"
        outcome = runner.run(["/bin/sh", "-c", f"trap '' TERM; touch {ready}; sleep 30"], 30, label="t", grace=5,
                             interrupted=lambda: ready.exists())
        try:
            self.assertFalse(outcome.gone)
            self.assertEqual(len(runner.leftovers), 1)
            self.assertIsNone(runner.leftovers[0].poll())
        finally:
            for process in runner.leftovers:  # the test's own clean-up: really kill it now
                os.killpg(process.pid, signal.SIGKILL)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(runner.settle(10))


class StorePrefix(unittest.TestCase):
    def test_a_sibling_that_only_starts_with_the_store_path_is_not_store(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store, sibling = tmp / "store", tmp / "store-evil"
        for directory in (store, sibling):
            directory.mkdir()
        (sibling / "tool").write_text("#!/bin/sh\n")
        (sibling / "tool").chmod(0o755)
        self.assertIn("entries", driver.directory_identity(str(sibling), str(store)))
        self.assertIn("sha256", driver.resolve_tool(str(sibling / "tool"), str(store)))


class RealRunner(unittest.TestCase):
    def test_bound_stops_the_child(self):
        started = time.monotonic()
        outcome = driver.Runner().run(["/bin/sh", "-c", "sleep 30"], 0.3, label="t", grace=1)
        self.assertEqual((outcome.reason, outcome.gone), ("bound", True))
        self.assertLess(time.monotonic() - started, 10)

    def test_interrupt_flag_stops_the_child_group(self):
        pid_file = Path(tempfile.mkdtemp()) / "pid"
        flag = {"set": False}
        threading.Timer(0.4, lambda: flag.update(set=True)).start()
        outcome = driver.Runner().run(["/bin/sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], 20, label="t",
                                      grace=2, interrupted=lambda: flag["set"])
        self.assertEqual((outcome.reason, outcome.gone), ("interrupted", True))
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_finished_child_with_a_grandchild_holding_the_pipe(self):
        started = time.monotonic()
        outcome = driver.Runner().run(["/bin/sh", "-c", "sleep 30 & echo done; exit 0"], 20, label="t", grace=1)
        self.assertEqual((outcome.code, outcome.reason, outcome.gone), (0, None, True))
        self.assertIn(b"done", outcome.output)
        self.assertLess(time.monotonic() - started, 8)

    def test_output_is_capped_while_reading(self):
        outcome = driver.Runner().run(["/bin/sh", "-c", "head -c 3000000 /dev/zero"], 20, label="t")
        self.assertEqual(outcome.code, 0)
        self.assertLessEqual(len(outcome.output), driver.OUTPUT_LIMIT)

    def test_exit_status_and_signal_deaths(self):
        self.assertEqual(tuple(driver.Runner().run(["/bin/sh", "-c", "echo hi; exit 3"], 5, label="t")), (3, b"hi\n"))
        self.assertEqual(driver.Runner().run(["/bin/sh", "-c", "kill -9 $$"], 5, label="t").code, -9)

    def test_a_child_that_cannot_be_signalled_is_not_called_gone(self):
        def denied(pgid, sig):
            if sig:
                raise PermissionError(1, "Operation not permitted")
            return os.killpg(pgid, sig)
        runner = driver.Runner(killpg=denied)
        outcome = runner.run(["/bin/sh", "-c", "sleep 2"], 0.3, label="t", grace=0.2)
        self.assertFalse(outcome.gone)
        self.assertTrue(runner.leftovers)
        self.assertTrue(runner.settle(10))

    def test_child_environment_is_exactly_what_is_passed(self):
        outcome = driver.Runner().run(["/usr/bin/env"], 5, label="t", env={"PATH": "/usr/bin:/bin", "TZ": "UTC"})
        self.assertEqual(sorted(outcome.output.decode().split()), ["PATH=/usr/bin:/bin", "TZ=UTC"])

    def test_wait_tcp(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        self.assertEqual(driver.wait_tcp("127.0.0.1", port, 5), 0)
        listener.close()
        ticks = iter(range(0, 100, 10))
        self.assertIsNone(driver.wait_tcp("127.0.0.1", port, 30, monotonic=lambda: next(ticks), sleep=lambda _: None))
        with self.assertRaises(lease.Rejected):
            driver.wait_tcp("host; x", 22, 1)

    def test_manifest_is_stable_and_sensitive(self):
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "a/f").write_text("1")
        (root / "l").symlink_to("a/f")
        first = driver.tree_manifest(root)
        self.assertEqual(first, driver.tree_manifest(root))
        (root / "a/f").chmod(0o755)
        self.assertNotEqual(first, driver.tree_manifest(root))
        self.assertEqual(subprocess.run([sys.executable, "-B", str(Path(driver.__file__)), "manifest", str(root)],
                                        capture_output=True, text=True, check=True).stdout.strip(),
                         driver.tree_manifest(root))


if __name__ == "__main__":
    unittest.main()
