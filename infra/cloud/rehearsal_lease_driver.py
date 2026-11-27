"""Plan, run and tear down the production lease (lease 6): one single-host app server, built from the existing tools.

The driver adds no provider or host logic of its own. Each step is a subprocess of an existing tool, called with its
documented arguments: `rehearsal_lease.py`, `rehearsal_timer.py`, `rehearsal_apply.py`,
`rehearsal_provider_actions.py`, `infra/nixos/stock_kexec.py`, `lease_install_plan.py`, `rescue_install.py`,
`rehearsal_sftp.py` and the host-steps driver `infra/nixos/lease_host_steps.py`. The driver writes only the inputs
those tools read, keeps the order, the guards and the evidence, and guarantees teardown.

Subcommands:

  manifest TREE                 digest of a staged tree (run sheet 0.4: compare on both machines)
  stage-project ...             copy the reviewed creator into RUN/project with 0600 files, then `bun install`
  plan ...                      validate every input, check the time window, print the ordered steps; contacts nothing
  run ... --approved-plan-sha   run steps 0.3 to 1.7, the host steps, then teardown (step 7) whatever happened
  resume-teardown ...           the recovery sequence for a lost runner: cleanup, inventory, disarm, final audit
  wait-tcp ADDRESS              (internal) wait for TCP 22 after the installed boot; no SSH, nothing trusted

Guards (docs/cloud-rehearsal.md, "Production lease driver"): stop at the first non-zero exit or failed guard, no
retry, straight to teardown (G1); the provider token only ever by file path and never opened here (G2); no SSH
without a pin (G3, in the tools); a kexec archive only from this revision's `infra/nixos/kexec-installer.nix` (G5);
no build inside the lease (G6); no overlap with 03:30 UTC +-15 min or 22:25-03:10 UTC (G7); only run-scoped
resources, through the tools' own selection (G8).

Exit codes: 0 pass; 1 a step failed and teardown was verified; 2 inputs refused, nothing contacted; 3 teardown not
verified: ESCALATE to a person, the expiry timer is left armed; 4 interrupted and teardown verified.
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

sys.dont_write_bytecode = True  # the staged tree's manifest must not change while the driver runs
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (HERE, ROOT / "infra/nixos"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import rehearsal_apply as apply  # noqa: E402
import rehearsal_lease as lease  # noqa: E402
import rescue_install  # noqa: E402
import lease_install_plan  # noqa: E402

# ---- Fixed decisions (orchestrator, 2026-11-26) --------------------------------------------------------------------

SERVER_TYPE = "cpx42"
RELAY_TYPE = "cpx22"        # required by the cohort input, unused by the single-host shape
LOCATION = "nbg1"
IMAGE = "debian-13"
PHASE = "original"
ROLE = "app"
DISK = "/dev/sda"
DISK_GB = 320
LEASE_SECONDS = 14400       # expiry = now + 14400, the timer armed before any create
PRE_ARM_SECONDS = 900       # run start to arming, worst case (checks, availability, read-only inventory)
TIMER_CLEANUP_SECONDS = 900  # the timer's own cleanup after expiry, worst case
WORST_CASE_SECONDS = PRE_ARM_SECONDS + LEASE_SECONDS + TIMER_CLEANUP_SECONDS
TEARDOWN_RESERVE = 900      # a step may start only if its bound plus this reserve plus the margin remain
MARGIN = 600
REFRESH_SECONDS = 40        # binding refresh period during pin, dispatch, verify, inspect, plan and install
HOST_GRACE = 240            # SIGTERM to the host steps, then this long for their own clean-up before SIGKILL
CHILD_GRACE = 30
SWEEP = (clock_time(3, 15), clock_time(3, 45), "sweep timer 03:30 UTC +-15 min")
BLOCK = (clock_time(22, 25), clock_time(3, 10), "arming block 22:25-03:10 UTC")
COST = {
    "serverType": SERVER_TYPE, "listMonthlyUsd": 81.99, "ipv4MonthlyUsd": 0.60, "hourlyUsdInferred": 0.112,
    "expectedUsd": 0.55, "capUsd": 5.0,
    "basis": "list prices in infra/cloud/budget.ts and docs/access-relay.md (2026-10-31), billed hourly up to the "
             "monthly price; 4 h x 0.112 + 19% VAT ~ 0.55 USD; 6 h still under 0.85 USD",
    "worstCase": "if the driver and the expiry timer both fail, the server bills up to ~82.59 USD/month plus VAT "
                 "until a person deletes it",
}
SYSTEMD = Path("/run/current-system/sw/bin")
EXIT_PASS, EXIT_STEP_FAILED, EXIT_INPUTS, EXIT_ESCALATE, EXIT_INTERRUPTED = 0, 1, 2, 3, 4
HOST_EXIT_MEANING = {0: "pass", 1: "a host step failed; its clean-up verified", 2: "inputs refused; nothing contacted",
                     3: "host-steps clean-up NOT verified: escalate", 4: "interrupted; its clean-up verified"}
OUTPUT_LIMIT = 256 * 1024
PREP_FILES = ("host-operator-ed25519", "host-operator-ed25519.pub", "deploy-key.sec", "deploy-key.pub",
              "spare-key.sec", "spare-key.pub", "escrow.age", "run-ca.pem", "sftp-client.pub", "lease-inputs.json",
              "lease-inputs.nix")
PREP_DIRECTORIES = ("secrets", "host-stage", "sftp")
FRESH_NAMES = ("lease.json", "closed.json", "lease-closed.json", "operator-ed25519", "rehearsal_lease.py",
               "foundation-input.json", "original-input.json", "host-known-hosts", "host-steps", "host-evidence",
               "driver", "preflight", "original-app-bootstrap-nonce")
# Deleted by exact path after a verified teardown: the token copy, every key, escrow, store, CA and Pulumi secret.
DELETE_FILES = ("provider-token", "operator-ed25519", "operator-ed25519.pub", "host-operator-ed25519",
                "host-operator-ed25519.pub", "deploy-key.sec", "deploy-key.pub", "spare-key.sec", "spare-key.pub",
                "escrow.age", "run-ca.pem", "sftp-client.pub", "original-app-bootstrap-nonce",
                "original-app-installed-ed25519", "foundation/passphrase.json", "original/passphrase.json",
                "foundation/plan.json", "original/plan.json")
DELETE_DIRECTORIES = ("secrets", "host-stage", "host-steps", "sftp", "foundation/backend", "original/backend",
                      "foundation/pulumi-home", "original/pulumi-home")


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


class Interrupted(BaseException):
    pass


# ---- Small helpers ---------------------------------------------------------------------------------------------------

def sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_manifest(root):
    """Every regular file and symlink under root, by path, with content digest and executable bit; sha256 of it."""
    root = Path(root)
    lines = []
    for directory, names, files in os.walk(root):
        names.sort()
        for name in sorted(files + [n for n in names if Path(directory, n).is_symlink()]):
            path = Path(directory, name)
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                lines.append(f"l {os.readlink(path)} {relative}")
            elif stat.S_ISREG(info.st_mode):
                lines.append(f"f {sha256_file(path)} {'x' if info.st_mode & 0o111 else '-'} {relative}")
            else:
                raise Refused(f"{relative}: unexpected file type in the staged tree")
        names[:] = [n for n in names if not Path(directory, n).is_symlink()]
    lines.sort(key=lambda line: line.rsplit(" ", 1)[-1])
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc)


def stamp(value):
    return utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")


def window_conflicts(start, end):
    """The forbidden spans (G7) that overlap [start, end); both aware UTC datetimes. Touching an edge is allowed."""
    hits = []
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        for begin, finish, name in (SWEEP, BLOCK):
            a = datetime.combine(day, begin, timezone.utc)
            b = datetime.combine(day + timedelta(days=1) if finish < begin else day, finish, timezone.utc)
            if start < b and a < end:
                hits.append(f"{name} ({a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M})")
        day += timedelta(days=1)
    return hits


def read_json(path, limit=4 * 1024 * 1024):
    return json.loads(lease.read_private(path, limit))


def private_run_directory(path):
    path = Path(path)
    if not re.fullmatch(r"[a-f0-9]{32}", path.name) or not re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(path)):
        raise Refused("the run directory must be an absolute path whose name is the 32-hex run id")
    try:
        return lease.private_directory(path)
    except (OSError, lease.Rejected):
        raise Refused("the run directory is not a private (0700, own, outside any Git checkout) directory") from None


def remove_inside(run, relative):
    """Delete one file, link or directory at RUN/relative by exact path; never follow a link out of RUN."""
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise Failed("deletion path rejected")
    real_run = Path(os.path.realpath(run))
    path = real_run / relative
    if os.path.realpath(path.parent) != str(path.parent):
        raise Failed(f"{relative}: parent is a link or outside the run directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise Failed("this platform cannot delete a tree without following links")
        shutil.rmtree(path)
    else:
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"\0" * info.st_size)
                stream.flush()
                os.fsync(stream.fileno())
        os.unlink(path)
    if os.path.lexists(path):
        raise Failed(f"{relative} still exists after deletion")
    return True


# ---- Configuration ---------------------------------------------------------------------------------------------------

@dataclass
class Config:
    run_directory: Path
    source: Path = ROOT
    python: str = sys.executable
    credential_file: Path = None
    revision: str = ""
    manifest_sha256: str = ""
    ssh_keygen: str = "ssh-keygen"
    nix: str = "nix"
    nix_store: str = "nix-store"
    kexec_archive: Path = None
    kexec_sha256: str = ""
    install_outputs: Path = None
    variants: Path = None
    ssh_cidr: str = ""
    unit_path: str = ""
    rclone: str = ""
    restic: str = ""
    sops: str = ""
    terrace_secrets: str = ""
    curl: str = None
    ssh: str = None
    expect_baseline: dict = field(default_factory=dict)
    min_free_bytes: int = 10 * 1024 ** 3
    queue_command: str = None
    store_dir: str = "/nix/store"
    user: str = None
    refresh_seconds: float = REFRESH_SECONDS

    def __post_init__(self):
        self.run_directory = Path(self.run_directory)
        self.source = Path(self.source)
        if self.credential_file is None:
            self.credential_file = self.run_directory / "provider-token"
        self.credential_file = Path(self.credential_file)
        if self.install_outputs is None:
            self.install_outputs = self.run_directory / "lease"
        if self.variants is None:
            self.variants = self.run_directory / "variants"
        if self.user is None:
            self.user = pwd.getpwuid(os.getuid()).pw_name

    @property
    def run(self):
        return self.run_directory.name

    def tool(self, *parts):
        return str(self.source.joinpath(*parts))

    def py(self, *parts):
        return [self.python, "-B", self.tool(*parts)]


def parse_baseline(text):
    counts = {kind: 0 for kind in lease.INVENTORY}
    for item in filter(None, (text or "").split(",")):
        kind, _, number = item.partition("=")
        if kind not in counts or not number.isdigit():
            raise Refused("--expect-baseline is KIND=N,... over the provider inventory kinds")
        counts[kind] = int(number)
    return counts


# ---- Subprocess runner -----------------------------------------------------------------------------------------------

class Runner:
    """Runs one child at a time in its own process group; records nothing itself. The driver keeps the evidence."""

    def __init__(self):
        self.current = None
        self.lock = threading.Lock()

    def run(self, argv, timeout, *, label, foreground=True, grace=CHILD_GRACE, cwd=None):
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, cwd=cwd)
        if foreground:
            with self.lock:
                self.current = process
        try:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.stop(process, grace)
                return 124, b"(bound reached; the driver stopped the child)\n"
            return process.returncode, output[:OUTPUT_LIMIT]
        except BaseException:
            self.stop(process, grace)
            raise
        finally:
            if foreground:
                with self.lock:
                    self.current = None

    @staticmethod
    def stop(process, grace):
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
            if process.poll() is not None:
                return
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                continue

    def abort_current(self):
        with self.lock:
            process = self.current
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# ---- Steps -----------------------------------------------------------------------------------------------------------

@dataclass
class Step:
    id: str
    title: str
    command: tuple = ()
    guard: str = ""
    expected: str = ""
    evidence: str = ""
    stop: str = "non-zero exit or failed guard: stop, no retry, teardown"
    bound: int = 60
    min_bound: int = None
    internal: str = None      # name of a Driver method for a step the driver does itself
    after: str = None         # name of a Driver method: the guard on the step's result
    unit: str = None          # the bounded systemd writer unit the tool requires
    refresh: bool = False     # keep the provider binding fresh while it runs
    host: bool = False        # the host-steps driver: its exit codes keep their meaning
    grace: int = CHILD_GRACE


def bounded_unit(cfg, unit, argv):
    """The bounded writer unit `rehearsal_apply.require_run_unit` checks: system slice, own MainPID, 230 s."""
    return ["sudo", "-n", str(SYSTEMD / "systemd-run"), "--quiet", "--collect", "--wait", "--pipe",
            "--unit=" + unit, "--uid=" + cfg.user, "--service-type=exec", "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=230", "--property=TimeoutStopSec=10s", "--property=UMask=0077",
            "--property=NoNewPrivileges=yes", "--setenv=PATH=" + cfg.unit_path, *argv]


def unit_names(run):
    prefix = f"terrace-cloud-{run}"
    return [f"{prefix}-foundation-preview.service", f"{prefix}-foundation-apply.service",
            f"{prefix}-original-preview.service", f"{prefix}-original-apply.service",
            f"{prefix}-original-app-apply.service"]


def run_steps(cfg):
    R, T = cfg.run_directory, str(cfg.credential_file)
    run = cfg.run
    lease_tool = cfg.py("infra", "cloud", "rehearsal_lease.py")
    timer_tool = cfg.py("infra", "cloud", "rehearsal_timer.py")
    apply_tool = cfg.py("infra", "cloud", "rehearsal_apply.py")
    actions_tool = cfg.py("infra", "cloud", "rehearsal_provider_actions.py")
    kexec_tool = cfg.py("infra", "nixos", "stock_kexec.py") + [str(R), PHASE, ROLE]
    host_tool = cfg.py("infra", "nixos", "lease_host_steps.py")
    creds = ["--directory", str(R), "--credential-file", T]
    availability = lease_tool + ["availability", *creds, "--server-type", SERVER_TYPE, "--location", LOCATION]
    binding = apply_tool + ["binding", PHASE, *creds, "--role", ROLE]
    host_run = host_tool + ["run", str(R), "--address", "{address}", "--evidence", str(R / "host-evidence"),
                            "--variants", str(cfg.variants), "--rclone", cfg.rclone, "--restic", cfg.restic,
                            "--sops", cfg.sops, "--terrace-secrets", cfg.terrace_secrets, "--nix", cfg.nix]
    if cfg.curl:
        host_run += ["--curl", cfg.curl]
    if cfg.ssh:
        host_run += ["--ssh", cfg.ssh]
    unit = f"terrace-cloud-{run}"
    return [
        Step("0.3", "builder idle, no unit of ours, no other open lease", internal="step_idle",
             guard="none", expected="no terrace-cloud-*/terrace-lease-sftp-* unit; queue command exit 0 if supplied",
             evidence="driver/receipts/0.3.json"),
        Step("0.4", "staged tree equals the approved revision", internal="step_manifest",
             guard="none", expected="manifest digest equal to the plan's", evidence="driver/receipts/0.4.json"),
        Step("0.6a", "bootstrap root key for the stock host", (cfg.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C",
             "terrace-lease-bootstrap", "-f", str(R / "operator-ed25519")),
             guard="file must not exist", expected="operator-ed25519 (0600) and .pub",
             evidence="file names only"),
        Step("0.6b", "lease inputs: proof nonce, foundation input, private lease tool copy", internal="step_inputs",
             guard="every file created new, 0600, inside the run directory",
             expected="original-app-bootstrap-nonce, foundation-input.json, rehearsal_lease.py",
             evidence="driver/receipts/0.6b.json (digests)"),
        Step("0.8a", f"availability of {SERVER_TYPE} in {LOCATION} (read-only)", tuple(availability),
             guard="G2 token by path", expected=f"{SERVER_TYPE} listed as available in a {LOCATION} datacenter",
             evidence="transcript", stop="not available: stop before anything is armed (nothing to tear down)"),
        Step("0.8b", "independent read-only inventory equals the stated baseline",
             tuple(lease_tool + ["prepare", "--directory", str(R / "preflight"), "--credential-file", T,
                                 "--run", "{preflight_run}", "--expires", "{preflight_expires}"]),
             guard="G8: read-only (prepare only lists)", expected="baseline counts equal --expect-baseline",
             evidence="preflight/lease.json", after="after_preflight", bound=120,
             stop="drift: stop before anything is armed"),
        Step("1.0", "time window at arming (G7) and expiry", internal="step_window",
             guard="G7", expected=f"[now, now+{LEASE_SECONDS + TIMER_CLEANUP_SECONDS}s] clear of the sweep and block",
             evidence="driver/receipts/1.0.json", stop="overlap: stop before anything is armed"),
        Step("1.1a", "prepare the lease (baseline, expiry now+14400)",
             tuple(lease_tool + ["prepare", *creds, "--run", run, "--expires", "{expires}"]),
             guard="1.0 passed", expected="lease.json with the same baseline as 0.8b",
             evidence="lease.json", after="after_prepare", bound=120),
        Step("1.1b", "arm the independent expiry cleanup timer", tuple(timer_tool + ["arm", *creds]),
             guard="lease.json", expected=f"terrace-cloud-{run}.timer at expiry", evidence="timer-armed.json"),
        Step("1.1c", "verify the armed timer", tuple(timer_tool + ["ready", *creds]),
             guard="none", expected="timer ready", evidence="transcript"),
        Step("1.2a", "foundation preview (run key, SSH-only firewall)",
             tuple(apply_tool + ["preview", "foundation", *creds]), unit=f"{unit}-foundation-preview.service",
             guard="timer armed", expected="create-only: key and firewall", evidence="foundation/review.json",
             after="after_foundation_preview", bound=240),
        Step("1.2b", "foundation create",
             tuple(apply_tool + ["create", "foundation", *creds, "--review-sha", "{review_foundation}",
                                 "--source-sha", "{source_sha}"]), unit=f"{unit}-foundation-apply.service",
             guard="reviewed digest, timer ready", expected="1 key, 1 firewall (TCP 22 from the build machine /32)",
             evidence="foundation-created.json", after="after_foundation_create", bound=240),
        Step("1.3a", "single-app cohort input", internal="step_cohort_input",
             guard="foundation IDs", expected="original-input.json: roles [app], CPX42, nbg1, debian-13, proof digest",
             evidence="driver/receipts/1.3a.json"),
        Step("1.3b", f"availability of {SERVER_TYPE} in {LOCATION} again, right before the preview", tuple(availability),
             guard="G2", expected="available", evidence="transcript",
             stop="not available: stop, teardown (only the foundation exists)"),
        Step("1.3c", "single-app cohort preview", tuple(apply_tool + ["preview", PHASE, *creds]),
             unit=f"{unit}-original-preview.service", guard="1.3b", expected="create-only: app server and its IPv4",
             evidence="original/review.json", after="after_cohort_preview", bound=240),
        Step("1.3d", "single-app cohort create",
             tuple(apply_tool + ["create", PHASE, *creds, "--review-sha", "{review_original}",
                                 "--source-sha", "{source_sha}"]), unit=f"{unit}-original-apply.service",
             guard="reviewed digest, timer ready", expected="1 server, 1 IPv4", evidence="original-created.json",
             after="after_cohort_create", bound=240,
             stop="any failure after dispatch leaves an unresolved dispatch: teardown escalates (timer stays armed)"),
        Step("1.4", "provider binding", tuple(binding), guard="none", expected="fresh binding (refreshed every 40 s)",
             evidence="original-app-binding-*.json", after="after_binding"),
        Step("1.5a", "pin the stock host key from its keyed proof", tuple(kexec_tool + ["--pin-proof"]),
             guard="HMAC proof", expected="pin written", evidence="original-app-bootstrap-proof.json",
             refresh=True, bound=360),
        Step("1.5b", "dispatch the RAM installer",
             tuple(kexec_tool + ["--dispatch", str(cfg.kexec_archive), cfg.kexec_sha256]),
             guard="G5 archive digest", expected="dispatch accepted", evidence="original-app-kexec-dispatched.json",
             refresh=True, bound=300),
        Step("1.5c", "verify the RAM installer", tuple(kexec_tool + ["--verify-ram"]),
             guard="same pin, new boot", expected="ready", evidence="original-app-ram-ready.json",
             refresh=True, bound=240),
        Step("1.5d", "install target and whole-disk receipt", tuple(kexec_tool + ["--inspect-target", str(DISK_GB)]),
             guard="none", expected=f"EFI, {DISK}", evidence="original-app-disk.json", refresh=True, bound=180),
        Step("1.6a", "install plan and installed host key",
             tuple(cfg.py("infra", "nixos", "lease_install_plan.py") + [
                 str(R), PHASE, ROLE, "--outputs", str(cfg.install_outputs), "--host-stage", str(R / "host-stage"),
                 "--host-public", str(R / "secrets/hosts/app/ssh_host_ed25519_key.pub")]),
             guard="disk receipt digest; G6 prestaged outputs", expected="plan digest printed and equal to the file",
             evidence="original-app-install.json", after="after_install_plan", refresh=True),
        Step("1.6b", "install V0 to the verified disk",
             tuple(cfg.py("infra", "nixos", "rescue_install.py") + [str(R), PHASE, ROLE, "--install-approved-sha",
                                                                    "{install_sha}"]),
             guard="approved plan digest", expected="Install status 0", evidence="original-app-installed.json",
             refresh=True, bound=2700),
        Step("1.7a", "boot the installed system", tuple(actions_tool + ["boot-installed", PHASE, ROLE, *creds]),
             unit=f"{unit}-original-app-apply.service", guard="installed receipt", expected="reboot dispatched",
             evidence="original-app-installed-boot.json", after="after_boot", bound=240),
        Step("1.7b", "TCP 22 answers on the installed system",
             tuple(cfg.py("infra", "cloud", "rehearsal_lease_driver.py") + ["wait-tcp", "{address}", "--seconds",
                                                                          "300"]),
             guard="no SSH, nothing trusted", expected="TCP 22 open", evidence="transcript (seconds)", bound=330,
             stop="no answer by the bound: stop, teardown"),
        Step("1.7c", "provider binding still names the installed address", tuple(binding),
             guard="none", expected="same server, same IPv4", evidence="binding", after="after_rebind"),
        Step("2.0", "pin the installed host key from the secrets store",
             tuple(host_tool + ["pin", str(R), "--address", "{address}"]),
             guard="G3", expected="host-known-hosts", evidence="host-known-hosts"),
        Step("2.1-6", "host steps 2.1 to 6 (lease_host_steps.py run)", tuple(host_run), host=True,
             guard="the host-steps driver's own guards", expected="exit 0",
             evidence="host-evidence/ (steps.log, receipts, summary.json, SHA256SUMS)", bound=7200, min_bound=3600,
             grace=HOST_GRACE, stop="exit 1/2/3/4 or the bound: teardown (exit 3 escalates)"),
    ]


TEARDOWN = (
    ("7.1", "let a running bounded writer unit finish (up to 240 s), then stop the run's five units by exact name; "
            "verify inactive", "sudo -n systemctl stop <the 5 exact terrace-cloud-RUN-* units>"),
    ("7.2", "stop the throwaway SFTP endpoint if its receipt exists and it was not stopped",
     "rehearsal_sftp.py stop --directory RUN/sftp"),
    ("7.3", "cleanup: delete run-owned resources, three absence reads, baseline preserved",
     "rehearsal_lease.py cleanup --directory RUN --credential-file TOKEN"),
    ("7.4", "independent read-only inventory equals the lease baseline (before disarm)",
     "rehearsal_lease.py prepare --directory RUN/audit --credential-file TOKEN --run <fresh> --expires <now+900>"),
    ("7.5", "disarm the expiry timer (only after 7.3 and 7.4)", "rehearsal_timer.py disarm --directory RUN --credential-file TOKEN"),
    ("7.6", "final audit from the receipts; timer and units inactive", "(internal)"),
    ("7.7", "delete token copy, keys, escrow, store, CA and Pulumi secrets by exact path inside RUN", "(internal)"),
    ("7.8", "builder: no unit of ours remains; closing receipt; open-lease pointer removed", "(internal)"),
)


# ---- Plan ------------------------------------------------------------------------------------------------------------

def display(cfg, step):
    if step.internal:
        return "(driver) " + step.title
    argv = list(step.command)
    if step.unit:
        argv = bounded_unit(cfg, step.unit, argv)
    return shlex.join(argv)


def check_inputs(cfg, runner, *, at_run=False):
    """Every refusal raises Refused. Returns (inputs, checks) for the plan. Contacts nothing outside this machine."""
    checks = []

    def ok(name, detail):
        checks.append({"check": name, "result": "pass", "detail": detail})

    R = private_run_directory(cfg.run_directory)
    for name in FRESH_NAMES:
        if os.path.lexists(R / name):
            raise Refused(f"{name} already exists: a lease that stops early is finished; use a new run directory")
    ok("run directory", "private, named by the run id, no earlier lease material")
    # Open lease pointers: any other run under the same root that is not closed.
    root = R.parent
    for entry in root.iterdir():
        if entry.name.endswith(".open") and entry.name != R.name + ".open":
            raise Refused(f"another lease is open ({entry.name})")
        if entry != R and entry.is_dir() and (entry / "lease.json").exists() and not (entry / "lease-closed.json").exists():
            raise Refused(f"another lease directory is not closed ({entry.name})")
    if os.path.lexists(root / (R.name + ".open")) and not at_run:
        raise Refused("this run's own open pointer exists: it already started")
    ok("open leases", "no other open lease pointer or unclosed lease directory beside this one")
    # Token: by path only; this program never opens it.
    token = cfg.credential_file
    try:
        info = token.lstat()
    except OSError:
        raise Refused("credential file missing") from None
    if (token.parent != R or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 16 <= info.st_size <= 4096):
        raise Refused("the credential file must be a 0600 copy directly inside the run directory (G2)")
    ok("credential copy", "0600 file inside the run directory; never opened by the driver")
    # Host-steps prepare layout (step 0.6).
    for name in PREP_FILES:
        if not (R / name).is_file() or (R / name).is_symlink():
            raise Refused(f"{name} missing: run lease_host_steps.py prepare first")
    for name in PREP_DIRECTORIES:
        if not (R / name).is_dir() or (R / name).is_symlink():
            raise Refused(f"{name}/ missing: run lease_host_steps.py prepare first")
    if not (R / "secrets/hosts/app/ssh_host_ed25519_key.pub").is_file():
        raise Refused("the secrets store holds no app host key")
    inputs_json = json.loads((R / "lease-inputs.json").read_text())
    if inputs_json.get("wombRevision") != cfg.revision or inputs_json.get("disk") != DISK:
        raise Refused("lease-inputs.json was prepared for another revision or disk")
    ok("host-steps prepare layout", "keys, escrow, store, run CA, SFTP, build inputs present")
    # Staged tree (0.4).
    if not re.fullmatch(r"[a-f0-9]{40}", cfg.revision) or not re.fullmatch(r"[a-f0-9]{64}", cfg.manifest_sha256):
        raise Refused("--revision must be a full commit and --source-manifest-sha256 a sha256")
    if os.path.lexists(cfg.source / ".git"):
        raise Refused("the source must be a staged `git archive`, not a checkout")
    manifest = tree_manifest(cfg.source)
    if manifest != cfg.manifest_sha256:
        raise Refused("the staged tree's manifest differs from the approved digest")
    ok("staged tree", f"manifest {manifest}")
    if not (cfg.source / "infra/nixos/lease_host_steps.py").is_file():
        raise Refused("infra/nixos/lease_host_steps.py is not in the staged tree (it lands with the host-steps PR)")
    # Creator project staged in RUN (0.4), bound to the reviewed source.
    source_sha = apply.fingerprint_source(lambda path: (cfg.source / "infra/cloud" / path).read_bytes())
    try:
        staged = apply.source_sha(R / "project/rehearsal-create")
    except (OSError, ValueError):
        raise Refused("RUN/project is not staged: run stage-project") from None
    if staged != source_sha:
        raise Refused("RUN/project differs from the staged tree's creator")
    ok("creator project", f"source {source_sha}")
    # G5: kexec archive from this revision's expression.
    archive = Path(os.path.realpath(cfg.kexec_archive)) if cfg.kexec_archive else None
    if archive is None or "diagnostic" in str(cfg.kexec_archive) + str(archive):
        raise Refused("G5: kexec archive refused (diagnostic or missing)")
    code, output = runner.run([cfg.nix, "eval", "--raw", "--offline", "--impure", "--file",
                               cfg.tool("infra", "nixos", "kexec-installer.nix"), "archive.outPath"],
                              600, label="plan-kexec-eval")
    expected = output.decode(errors="replace").strip()
    if code or not rescue_install.STORE.fullmatch(expected):
        raise Refused("G5: could not evaluate infra/nixos/kexec-installer.nix at the staged revision")
    store = Path(os.path.realpath(cfg.store_dir))
    if archive != store / Path(expected).name / "nixos-kexec-installer-x86_64-linux.tar.gz":
        raise Refused("G5: the kexec archive is not this revision's infra/nixos/kexec-installer.nix output")
    if not re.fullmatch(r"[a-f0-9]{64}", cfg.kexec_sha256) or sha256_file(archive) != cfg.kexec_sha256:
        raise Refused("G5: kexec archive digest differs")
    ok("kexec archive (G5)", f"{expected} sha256 {cfg.kexec_sha256}")
    # G6: prestaged install and variants, valid in the store and rooted in RUN.
    try:
        outputs = lease_install_plan.read_outputs(cfg.install_outputs, ROLE, cfg.store_dir)
    except (OSError, ValueError):
        raise Refused("G6: install outputs are not lease-host.nix's `install` for the app") from None
    if outputs["device"] != DISK:
        raise Refused("G6: install outputs format another disk")
    try:
        variants = json.loads(Path(cfg.variants).read_text())
    except (OSError, ValueError):
        raise Refused("G6: variants file unreadable") from None
    names = ("v0", "v1", "release", "v2", "v3", "v4", "v5")
    if (not isinstance(variants, dict) or set(variants) != set(names) or variants["v0"] != outputs["system"]
            or not all(isinstance(v, str) and rescue_install.STORE.fullmatch(v) for v in variants.values())):
        raise Refused("G6: variants are not lease-host.nix's `variants` for this install")
    paths = [outputs["system"], outputs["disko"], outputs["installer"], *(variants[n] for n in names[1:])]
    code, _ = runner.run([cfg.nix_store, "--check-validity", *paths], 120, label="plan-store-validity")
    if code:
        raise Refused("G6: a prestaged store path is missing (a build would be needed inside the lease)")
    for link in (cfg.install_outputs, cfg.variants):
        code, output = runner.run([cfg.nix_store, "--query", "--roots", os.path.realpath(link)], 120,
                                  label="plan-store-roots")
        if code or not any(line.startswith(f"{link} -> ") for line in output.decode(errors="replace").splitlines()):
            raise Refused(f"G6: {Path(link).name} is not a GC root inside the run directory")
    ok("prestaged closures (G6)", f"{len(paths)} store paths valid, rooted by RUN/lease and RUN/variants")
    # Tools and inputs.
    for name in ("rclone", "restic", "sops", "terrace_secrets"):
        value = getattr(cfg, name)
        if not value or not os.access(value, os.X_OK):
            raise Refused(f"--{name.replace('_', '-')} must be an executable")
    if not re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}/32", cfg.ssh_cidr or ""):
        raise Refused("--ssh-cidr must be the build machine's IPv4 /32 (G4)")
    if not cfg.unit_path or not all(Path(part).is_absolute() for part in cfg.unit_path.split(":")):
        raise Refused("--unit-path must be absolute directories (pulumi and node for the bounded units)")
    if set(cfg.expect_baseline) != set(lease.INVENTORY):
        raise Refused("--expect-baseline missing")
    ok("tools and inputs", f"SSH source {cfg.ssh_cidr}; bounded-unit PATH given")
    free = shutil.disk_usage(R).free
    if free < cfg.min_free_bytes:
        raise Refused(f"only {free} bytes free under the run directory; need {cfg.min_free_bytes}")
    checks.append({"check": "free space", "result": "pass", "detail": f"{free // 1024 ** 3} GiB free"})
    checks.extend(idle_checks(cfg, runner))
    inputs = {"run": cfg.run, "revision": cfg.revision, "manifestSha256": cfg.manifest_sha256,
              "sourceSha256": source_sha, "kexecArchive": str(archive), "kexecSha256": cfg.kexec_sha256,
              "install": {k: outputs[k] for k in ("system", "disko", "installer", "device")}, "variants": variants,
              "sshCidr": cfg.ssh_cidr, "expectBaseline": cfg.expect_baseline, "serverType": SERVER_TYPE,
              "location": LOCATION, "image": IMAGE, "leaseSeconds": LEASE_SECONDS,
              "queueCommand": cfg.queue_command}
    return inputs, checks


def idle_checks(cfg, runner):
    checks = []
    for scope in ([], ["--user"]):
        code, output = runner.run([str(SYSTEMD / "systemctl"), *scope, "list-units", "--plain",
                                   "--no-legend", "terrace-cloud-*", "terrace-lease-sftp-*"], 30, label="idle-units")
        if code or output.strip():
            raise Refused(f"builder not idle: {'user' if scope else 'system'} units of a lease are loaded")
    checks.append({"check": "builder units", "result": "pass", "detail": "no terrace-cloud-*/terrace-lease-sftp-* unit"})
    if cfg.queue_command:
        code, _ = runner.run([cfg.queue_command], 60, label="idle-queue")
        if code:
            raise Refused(f"heavy-queue check {cfg.queue_command} exited {code}: not idle")
        checks.append({"check": "heavy queue", "result": "pass", "detail": f"supplied: {cfg.queue_command} exit 0"})
    else:
        checks.append({"check": "heavy queue", "result": "NOT MADE",
                       "detail": "no --queue-idle-command supplied: the heavy-queue check was not made"})
    return checks


def make_plan(cfg, runner, start, *, at_run=False):
    inputs, checks = check_inputs(cfg, runner, at_run=at_run)
    end = start + WORST_CASE_SECONDS
    conflicts = window_conflicts(utc(start), utc(end))
    if conflicts:
        raise Refused("G7: the planned lease overlaps " + "; ".join(conflicts))
    steps = [{"id": s.id, "title": s.title, "command": display(cfg, s), "guard": s.guard, "expected": s.expected,
              "evidence": s.evidence, "stop": s.stop, "boundSeconds": s.bound} for s in run_steps(cfg)]
    teardown = [{"id": i, "title": t, "command": c} for i, t, c in TEARDOWN]
    return {"schema": 1, "inputs": inputs, "steps": steps, "teardown": teardown, "cost": COST,
            "window": {"plannedStart": stamp(start), "worstCaseSeconds": WORST_CASE_SECONDS,
                       "plannedEnd": stamp(end), "expiresAt": stamp(start + PRE_ARM_SECONDS + LEASE_SECONDS),
                       "forbidden": [SWEEP[2], BLOCK[2]]},
            "checks": checks}


def approval_view(plan):
    return {key: plan[key] for key in ("schema", "inputs", "steps", "teardown", "cost")}


def render_plan(plan, digest):
    lines = [f"LEASE 6 PLAN for run {plan['inputs']['run']} at revision {plan['inputs']['revision']}",
             f"Server: {SERVER_TYPE} in {LOCATION}, {IMAGE} then kexec, one IPv4, firewall TCP 22 from "
             f"{plan['inputs']['sshCidr']} only",
             f"Window (G7): start {plan['window']['plannedStart']}, worst case {WORST_CASE_SECONDS} s, end "
             f"{plan['window']['plannedEnd']}; expiry about {plan['window']['expiresAt']} (now+{LEASE_SECONDS} at arming)",
             f"Cost: expected about {COST['expectedUsd']} USD, cap {COST['capUsd']} USD. {COST['worstCase']}.",
             "Checks:"]
    lines += [f"  [{c['result']}] {c['check']}: {c['detail']}" for c in plan["checks"]]
    lines.append("Steps (stop at the first failure, no retry, then teardown):")
    for step in plan["steps"]:
        lines += [f"  {step['id']}  {step['title']}  (bound {step['boundSeconds']} s)",
                  f"       command:  {step['command']}", f"       guard:    {step['guard']}",
                  f"       expected: {step['expected']}", f"       evidence: {step['evidence']}",
                  f"       stop:     {step['stop']}"]
    lines.append("Teardown (always, once anything was armed or created):")
    lines += [f"  {t['id']}  {t['title']}\n       command:  {t['command']}" for t in plan["teardown"]]
    lines.append(f"Approve with: --approved-plan-sha {digest}")
    return "\n".join(lines) + "\n"


# ---- Run -------------------------------------------------------------------------------------------------------------

class Driver:
    def __init__(self, cfg, runner, *, out=sys.stdout, err=sys.stderr, now=time.time, sleep=time.sleep,
                 evidence_name="driver"):
        self.sleep = sleep
        self.cfg = cfg
        self.runner = runner
        self.out = out
        self.err = err
        self.now = now
        self.R = cfg.run_directory
        self.evidence = self.R / evidence_name
        self.ctx = {}
        self.refresher = None
        self.refresh_stop = threading.Event()
        self.refresh_failed = False
        self.refreshes = 0
        self.interrupted = False
        self.teardown_running = False
        self.host_exit = None
        self.local_clean = True

    # Evidence
    def log(self, line):
        with open(self.evidence / "steps.log", "a") as stream:
            stream.write(line + "\n")

    def receipt(self, name, data):
        lease.write_private(self.evidence / "receipts" / f"{name}.json", data)

    def say(self, text, stream=None):
        try:
            print(text, file=stream or self.out, flush=True)
        except (OSError, ValueError):
            pass  # a lost terminal must never skip teardown

    # Signals
    def on_signal(self, signum, _frame):
        if self.teardown_running or self.interrupted:
            self.say(f"lease driver: signal {signum} ignored; teardown in progress or already requested", self.err)
            return
        self.interrupted = True
        raise Interrupted()

    # Steps
    def resolve(self, argv):
        try:
            return [part.format_map(self.ctx) for part in argv]
        except KeyError as missing:
            raise Failed(f"value {missing} not yet known") from None

    def expires(self):
        return read_json(self.R / "lease.json")["expires"]

    def execute(self, step):
        started = self.now()
        record = {"step": step.id, "title": step.title, "started": int(started), "bound": step.bound}
        try:
            bound = step.bound
            if os.path.lexists(self.R / "lease.json"):
                remaining = self.expires() - started - TEARDOWN_RESERVE - MARGIN
                bound = record["bound"] = min(step.bound, int(remaining))
                if bound < (step.min_bound or step.bound):
                    raise Failed(f"deadline: {int(remaining)} s left before the teardown reserve, step needs "
                                 f"{step.min_bound or step.bound}")
            if step.internal:
                record["command"] = "(driver)"
                record["result"] = getattr(self, step.internal)() or {}
                record["exit"] = 0
            else:
                self.set_refresher(step.refresh)
                argv = self.resolve(step.command)
                if step.unit:
                    argv = bounded_unit(self.cfg, step.unit, argv)
                    self.ctx.setdefault("units_started", []).append(step.unit)
                record["command"] = shlex.join(argv)
                code, output = self.runner.run(argv, bound, label=step.id, grace=step.grace)
                record["exit"] = code
                (self.evidence / "transcripts" / f"{step.id}.log").write_bytes(output)
                if step.host:
                    self.host_exit = code
                    record["hostExitMeaning"] = HOST_EXIT_MEANING.get(code, "unknown")
                if self.refresh_failed:
                    raise Failed("binding refresh failed while the step ran")
                if code:
                    raise Failed(f"exit {code}")
                if step.after:
                    record["result"] = getattr(self, step.after)(output) or {}
        except Failed as failure:
            record.update(status="failed", error=str(failure))
            raise
        except Interrupted:
            record.update(status="interrupted")
            raise
        except Exception as problem:  # noqa: BLE001 - any other error fails the step by its type only
            record.update(status="failed", error=f"{type(problem).__name__} in the driver")
            raise Failed(record["error"]) from None
        else:
            record["status"] = "passed"
        finally:
            record["seconds"] = round(self.now() - started, 1)
            self.receipt(step.id, record)
            self.log(f"{step.id} {record.get('status', 'failed')} exit {record.get('exit', '-')} "
                     f"{record['seconds']}s {step.title}")
            self.say(f"[{step.id}] {record.get('status', 'failed')}: {step.title}"
                     + (f" ({record['error']})" if record.get("error") else ""))

    # Binding refresher
    def set_refresher(self, wanted):
        if wanted and self.refresher is None:
            self.refresh_stop.clear()
            self.refresher = threading.Thread(target=self.refresh_loop, daemon=True)
            self.refresher.start()
        elif not wanted:
            self.stop_refresher()

    def stop_refresher(self):
        if self.refresher is not None:
            self.refresh_stop.set()
            self.refresher.join(90)
            self.refresher = None

    def refresh_loop(self):
        argv = self.cfg.py("infra", "cloud", "rehearsal_apply.py") + [
            "binding", PHASE, "--directory", str(self.R), "--credential-file", str(self.cfg.credential_file),
            "--role", ROLE]
        while not self.refresh_stop.wait(self.cfg.refresh_seconds):
            code, _ = self.runner.run(argv, 60, label="1.4-refresh", foreground=False)
            self.refreshes += 1
            if code:
                self.refresh_failed = True
                self.runner.abort_current()
                return

    # Internal steps
    def step_idle(self):
        return {"checks": idle_checks(self.cfg, self.runner)}

    def step_manifest(self):
        digest = tree_manifest(self.cfg.source)
        if digest != self.cfg.manifest_sha256:
            raise Failed("staged tree changed since the plan")
        return {"manifestSha256": digest}

    def step_inputs(self):
        public = (self.R / "operator-ed25519.pub").read_text().split()
        if public[:1] != ["ssh-ed25519"] or len(public) < 2:
            raise Failed("bootstrap key is not ed25519")
        self.ctx["bootstrap_public"] = " ".join(public[:2])
        nonce_sha = rescue_install.write_proof_nonce(self.R / "original-app-bootstrap-nonce")
        self.ctx["nonce_sha"] = nonce_sha
        copy = self.R / "rehearsal_lease.py"
        rescue_install.write_new_text(copy, (self.cfg.source / "infra/cloud/rehearsal_lease.py").read_text())
        lease.write_private(self.R / "foundation-input.json", {
            "run": self.cfg.run, "phase": "foundation", "sshPublicKey": self.ctx["bootstrap_public"],
            "sshCidrs": [self.cfg.ssh_cidr]})
        return {"nonceSha256": nonce_sha, "leaseToolSha256": sha256_file(copy)}

    def after_preflight(self, _output):
        baseline = read_json(self.R / "preflight/lease.json")["baseline"]
        counts = {kind: len(rows) for kind, rows in baseline.items()}
        if counts != self.cfg.expect_baseline:
            raise Failed(f"inventory drift: {counts} differs from the stated baseline")
        self.ctx["preflight_baseline"] = baseline
        return {"counts": counts}

    def step_window(self):
        now = self.now()
        conflicts = window_conflicts(utc(now), utc(now + LEASE_SECONDS + TIMER_CLEANUP_SECONDS))
        if conflicts:
            raise Failed("G7: arming now would overlap " + "; ".join(conflicts))
        self.ctx["expires"] = str(int(now) + LEASE_SECONDS)
        return {"expires": int(self.ctx["expires"]), "expiresAt": stamp(int(self.ctx["expires"]))}

    def after_prepare(self, _output):
        manifest = read_json(self.R / "lease.json")
        if manifest["run"] != self.cfg.run or str(manifest["expires"]) != self.ctx["expires"]:
            raise Failed("lease.json is for another run or expiry")
        if manifest["baseline"] != self.ctx["preflight_baseline"]:
            raise Failed("the lease baseline differs from the read-only inventory")
        return {"expiresAt": stamp(manifest["expires"])}

    def review(self, phase, targets, roles):
        path = self.R / phase / "review.json"
        data = lease.read_private(path, 65536)
        review = json.loads(data)
        if sorted(review["creates"]) != sorted(targets) or review["roles"] != roles \
                or review["sourceSha256"] != self.ctx["source_sha"]:
            raise Failed(f"{phase} preview scope differs from the plan")
        return hashlib.sha256(data).hexdigest()

    def after_foundation_preview(self, _output):
        targets = apply.phase_targets("foundation", self.cfg.run)
        self.ctx["review_foundation"] = self.review("foundation", targets, [])
        return {"reviewSha256": self.ctx["review_foundation"]}

    def after_foundation_create(self, _output):
        created = read_json(self.R / "foundation-created.json")
        if created["servers"] or created["primary_ips"] or len(created["firewalls"]) != 1 or len(created["ssh_keys"]) != 1:
            raise Failed("foundation created another set")
        self.ctx["firewall_id"], self.ctx["key_id"] = created["firewalls"][0], created["ssh_keys"][0]
        return created

    def step_cohort_input(self):
        config = {"run": self.cfg.run, "phase": PHASE, "sshKeyId": self.ctx["key_id"],
                  "firewallId": self.ctx["firewall_id"], "image": IMAGE, "location": LOCATION,
                  "appType": SERVER_TYPE, "relayType": RELAY_TYPE, "roles": [ROLE],
                  "bootstrapProofs": {ROLE: {"nonceSha256": self.ctx["nonce_sha"]}},
                  "bootstrapOperatorPublicKey": self.ctx["bootstrap_public"]}
        lease.write_private(self.R / "original-input.json", config)
        return {"inputSha256": sha256_file(self.R / "original-input.json")}

    def after_cohort_preview(self, _output):
        targets = apply.phase_targets(PHASE, self.cfg.run, single_app=True)
        self.ctx["review_original"] = self.review(PHASE, targets, [ROLE])
        return {"reviewSha256": self.ctx["review_original"]}

    def after_cohort_create(self, _output):
        created = read_json(self.R / "original-created.json")
        if len(created["servers"]) != 1 or len(created["primary_ips"]) != 1 or created["firewalls"] or created["ssh_keys"]:
            raise Failed("cohort created another set")
        return created

    def newest_binding(self):
        return rescue_install.latest_binding(self.R, f"{PHASE}-{ROLE}")

    def after_binding(self, _output):
        binding = self.newest_binding()
        created = read_json(self.R / "original-created.json")
        if binding["serverId"] not in created["servers"] or binding["run"] != self.cfg.run:
            raise Failed("binding names another server")
        return {"serverId": binding["serverId"]}

    def after_install_plan(self, output):
        match = re.search(rb"--install-approved-sha ([a-f0-9]{64})\b", output)
        plan_file = self.R / f"{PHASE}-{ROLE}-install.json"
        plan = read_json(plan_file)
        install = {k: v for k, v in self.ctx["install"].items() if k != "device"}
        if match is None or match[1].decode() != sha256_file(plan_file):
            raise Failed("the printed plan digest is not the plan file's")
        if {k: plan[k] for k in install} != install:
            raise Failed("the install plan names other store paths than the approved plan (G6)")
        self.ctx["install_sha"] = match[1].decode()
        return {"installSha256": self.ctx["install_sha"]}

    def after_boot(self, _output):
        installed = read_json(self.R / f"{PHASE}-{ROLE}-installed.json")
        self.ctx["address"] = installed["binding"]["ipv4"]
        self.ctx["server_id"] = installed["binding"]["serverId"]
        return {"serverId": self.ctx["server_id"]}

    def after_rebind(self, _output):
        binding = self.newest_binding()
        if (binding["serverId"], binding["ipv4"]) != (self.ctx["server_id"], self.ctx["address"]):
            raise Failed("the provider binding no longer names the installed server and address")
        return {"sameServer": True}

    # The run
    def start(self, approved_sha):
        plan_path = self.R / "lease-plan.json"
        try:
            data = lease.read_private(plan_path, 1024 * 1024)
        except (OSError, lease.Rejected):
            raise Refused("lease-plan.json missing: run plan first") from None
        if hashlib.sha256(data).hexdigest() != approved_sha:
            raise Refused("the approved digest is not lease-plan.json's")
        approved = json.loads(data)
        current = make_plan(self.cfg, self.runner, self.now())
        if approval_view(current) != approval_view(approved):
            raise Refused("inputs or steps changed since the plan was approved")
        return current

    def run(self, approved_sha):
        try:
            plan = self.start(approved_sha)
        except Refused as refusal:
            self.say(f"lease driver: refused: {refusal}", self.err)
            return EXIT_INPUTS
        pointer = self.R.parent / (self.cfg.run + ".open")
        descriptor = os.open(pointer, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.write(descriptor, (str(self.R) + "\n").encode())
        os.close(descriptor)
        self.evidence.mkdir(mode=0o700)
        (self.evidence / "receipts").mkdir(mode=0o700)
        (self.evidence / "transcripts").mkdir(mode=0o700)
        self.ctx.update(source_sha=plan["inputs"]["sourceSha256"], install=plan["inputs"]["install"],
                        preflight_run=secrets.token_hex(16), preflight_expires=str(int(self.now()) + 1800))
        (self.R / "preflight").mkdir(mode=0o700)
        self.log(f"run start {stamp(self.now())} revision {self.cfg.revision}")
        failed = None
        crashed = False
        handlers = {sig: signal.signal(sig, self.on_signal) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        steps = run_steps(self.cfg)
        current = None
        try:
            for current in steps:
                self.execute(current)
        except Failed:
            failed = current.id if current else "start"
        except Interrupted:
            self.interrupted = True
            failed = current.id if current else "start"
        except BaseException:  # noqa: BLE001 - a crash still tears down
            crashed = True
            failed = current.id if current else "start"
        finally:
            # Signals stay with on_signal, which ignores them from here on, until the closing receipt is written.
            self.teardown_running = True
            try:
                verified = self.teardown()
                if not verified or self.host_exit == 3 or not self.local_clean:
                    code = EXIT_ESCALATE
                elif self.interrupted:
                    code = EXIT_INTERRUPTED
                elif failed or crashed:
                    code = EXIT_STEP_FAILED
                else:
                    code = EXIT_PASS
                self.close(code, failed, verified, pointer)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
        return code

    # Teardown (step 7)
    def tear(self, step_id, title, argv, bound, *, label=None):
        started = self.now()
        try:
            code, output = self.runner.run(argv, bound, label=label or step_id)
        except Interrupted:
            code, output = 130, b""
        (self.evidence / "transcripts" / f"{step_id}.log").write_bytes(output)
        record = {"step": step_id, "title": title, "command": shlex.join(argv), "exit": code,
                  "seconds": round(self.now() - started, 1)}
        self.receipt(step_id, record)
        self.log(f"{step_id} {'passed' if code == 0 else 'failed'} exit {code} {record['seconds']}s {title}")
        self.say(f"[{step_id}] {'passed' if code == 0 else 'FAILED'}: {title}")
        return code, output

    def teardown(self):
        self.teardown_running = True
        self.stop_refresher()
        self.runner.abort_current()
        cfg, R = self.cfg, self.R
        result = self.teardown_result = {"providerVerified": False, "timerDisarmed": False, "deleted": []}
        try:
            if not os.path.lexists(R / "lease.json"):
                # Nothing was prepared, armed or created: no provider teardown exists to do.
                self.log("teardown: no lease.json, nothing armed or created")
                result.update(providerVerified=True, nothingArmed=True)
                self.local_clean = self.local_units_clear()
                return True
            units = unit_names(cfg.run)
            # A create cut off mid-apply leaves an unresolved dispatch that cleanup cannot close, so let a running
            # writer unit finish (it is bounded at 230 s) and record what it created, then stop whatever remains.
            deadline = self.now() + 240
            while True:
                active = [unit for unit in units if self.unit_state(unit, "7.1-wait") not in ("inactive", "failed", "")]
                if not active or self.now() >= deadline:
                    break
                self.sleep(5)
            self.tear("7.1", "stop bounded writer units", ["sudo", "-n", str(SYSTEMD / "systemctl"), "stop", *units], 60)
            for unit in units:
                code, output = self.runner.run([str(SYSTEMD / "systemctl"), "show", unit, "--property=ActiveState",
                                                "--value"], 15, label="7.1-show")
                if output.decode(errors="replace").strip() not in ("inactive", "failed", ""):
                    self.say(f"unit {unit} is still active", self.err)
                    return False
            sftp = R / "sftp"
            if (sftp / "sftp-listener.json").exists() and not (sftp / "sftp-stopped.json").exists():
                code, _ = self.tear("7.2", "stop the SFTP endpoint",
                                    cfg.py("infra", "cloud", "rehearsal_sftp.py") + ["stop", "--directory", str(sftp)], 60)
                if code:
                    self.local_clean = False
            creds = ["--directory", str(R), "--credential-file", str(cfg.credential_file)]
            code, _ = self.tear("7.3", "cleanup with three absence reads",
                                cfg.py("infra", "cloud", "rehearsal_lease.py") + ["cleanup", *creds], 420)
            if code:
                return False
            audit = self.audit_dir = R / f"audit-{time.time_ns()}"
            audit.mkdir(mode=0o700)
            code, _ = self.tear("7.4", "independent read-only inventory",
                                cfg.py("infra", "cloud", "rehearsal_lease.py") + [
                                    "prepare", "--directory", str(audit), "--credential-file", str(cfg.credential_file),
                                    "--run", secrets.token_hex(16), "--expires", str(int(self.now()) + 900)], 120)
            if code or not self.inventory_equal():
                self.say("independent inventory differs from the lease baseline", self.err)
                return False
            code, _ = self.tear("7.5", "disarm the expiry timer",
                                cfg.py("infra", "cloud", "rehearsal_timer.py") + ["disarm", *creds], 60)
            if code:
                return False
            result["timerDisarmed"] = True
            audit_result = self.final_audit()
            result["finalAudit"] = audit_result
            if not audit_result["pass"]:
                return False
            result["providerVerified"] = True
            result["deleted"] = self.delete_material()
            self.local_clean = self.local_units_clear() and self.local_clean
            return True
        except Exception as problem:  # noqa: BLE001 - any error in teardown means not verified: escalate
            self.say(f"teardown error: {type(problem).__name__}", self.err)
            return False

    def unit_state(self, unit, label):
        _, output = self.runner.run([str(SYSTEMD / "systemctl"), "show", unit, "--property=ActiveState", "--value"], 15,
                                    label=label)
        return output.decode(errors="replace").strip()

    def inventory_equal(self):
        audit = read_json(self.audit_dir / "lease.json")["baseline"]
        return audit == read_json(self.R / "lease.json")["baseline"]

    def final_audit(self):
        R, run = self.R, self.cfg.run
        facts = {"closed": (R / "closed.json").is_file()}
        receipts = sorted(R.glob("final-absence-*.json"))
        absence = read_json(receipts[-1]) if receipts else {}
        facts["absence"] = (absence.get("run") == run and absence.get("phase") == "final"
                            and absence.get("baselinePreserved") is True and absence.get("absenceReads") == 3
                            and absence.get("unresolvedDispatches") == [])
        facts["inventoryEqual"] = self.inventory_equal()
        preflight = R / "preflight/lease.json"
        facts["preflightEqual"] = (not preflight.exists()
                                   or read_json(preflight)["baseline"] == read_json(R / "lease.json")["baseline"])
        states = []
        for unit in (f"terrace-cloud-{run}.timer", f"terrace-cloud-{run}.service", *unit_names(run)):
            _, output = self.runner.run([str(SYSTEMD / "systemctl"), "show", unit, "--property=ActiveState", "--value"],
                                        15, label="7.6-show")
            states.append(output.decode(errors="replace").strip())
        facts["unitsInactive"] = all(state in ("inactive", "failed", "") for state in states)
        facts["pass"] = all(facts.values())
        lease.write_private(self.evidence / "final-audit.json", facts)
        self.say(f"[7.6] {'passed' if facts['pass'] else 'FAILED'}: final audit")
        return facts

    def delete_material(self):
        deleted = []
        for name in DELETE_FILES + DELETE_DIRECTORIES:
            if name == "sftp" and not self.local_clean:
                continue  # the endpoint may still serve it; a person stops it first
            if remove_inside(self.R, name):
                deleted.append(name)
        if os.path.lexists(self.cfg.credential_file):
            raise Failed("the token copy still exists")
        self.log(f"7.7 deleted {len(deleted)} paths: {' '.join(deleted)}")
        return deleted

    def local_units_clear(self):
        try:
            idle_checks(Config(run_directory=self.R, source=self.cfg.source, user=self.cfg.user), self.runner)
            return True
        except Refused:
            self.say("units of a lease remain loaded on this machine", self.err)
            return False

    def close(self, code, failed, verified, pointer):
        summary = {"run": self.cfg.run, "exit": code, "failedStep": failed, "interrupted": self.interrupted,
                   "hostStepsExit": self.host_exit,
                   "hostStepsMeaning": HOST_EXIT_MEANING.get(self.host_exit) if self.host_exit is not None else None,
                   "teardown": getattr(self, "teardown_result", {}), "bindingRefreshes": self.refreshes,
                   "closedAt": stamp(self.now())}
        lease.write_private(self.evidence / "closing.json", summary)
        if verified and code != EXIT_ESCALATE:
            lease.write_private(self.R / "lease-closed.json", summary)
            try:
                pointer.unlink()
            except FileNotFoundError:
                pass
        sums = []
        for path in sorted(p for p in self.evidence.rglob("*") if p.is_file()):
            sums.append(f"{sha256_file(path)}  {path.relative_to(self.evidence)}")
        (self.evidence / "SHA256SUMS").write_text("\n".join(sums) + "\n")
        if code == EXIT_ESCALATE:
            self.say(recovery_text(self.cfg, summary), self.err)
        else:
            self.say(f"lease driver: exit {code} ({['pass', 'step failed, teardown verified', '', '', 'interrupted, teardown verified'][code]}); "
                     f"evidence in {self.evidence}")


def recovery_text(cfg, summary):
    R, T, run = cfg.run_directory, cfg.credential_file, cfg.run
    py = f"{cfg.python} -B {cfg.source}"
    creds = f"--directory {R} --credential-file {T}"
    return "\n".join([
        "=" * 78,
        "ESCALATE: teardown could NOT be verified. A person must act now.",
        f"Run {run}: failed step {summary.get('failedStep')}; host steps exit {summary.get('hostStepsExit')}.",
        f"The expiry timer terrace-cloud-{run}.timer is LEFT ARMED; it runs the cleanup at the lease expiry.",
        "Nothing was deleted unless the teardown summary lists it: the token copy and keys stay for recovery.",
        "Recovery, on this machine, from the staged tree:",
        f"  {py}/infra/cloud/rehearsal_lease_driver.py resume-teardown --run-directory {R} "
        f"--credential-file {T} --source {cfg.source}",
        "or the same steps one by one, stopping at the first failure:",
        f"  {py}/infra/cloud/rehearsal_lease.py cleanup {creds}",
        f"  mkdir -m 0700 {R}/audit-N && {py}/infra/cloud/rehearsal_lease.py prepare --directory {R}/audit-N "
        f"--credential-file {T} --run $(openssl rand -hex 16) --expires $(( $(date +%s) + 900 ))",
        f"      then compare the baseline in {R}/audit-N/lease.json with {R}/lease.json",
        f"  {py}/infra/cloud/rehearsal_timer.py disarm {creds}",
        "If cleanup reports an unresolved dispatch (a create failed or was cut off after dispatch), disarm stays",
        "refused by design. Leave the timer armed; after it has fired at expiry, run the inventory above. If it",
        "equals the baseline, nothing remains: check `systemctl show terrace-cloud-<run>.timer` is not loaded,",
        "then delete the run material by exact path (resume-teardown --delete-after-person-check does exactly that).",
        "If the inventory shows a run resource, delete it in the provider console by its exact name",
        f"terrace-r-{run}-..., never anything else, and write down what you did.",
        "=" * 78,
    ])


def resume_teardown(cfg, runner, *, delete_after_person_check=False, out=sys.stdout, err=sys.stderr):
    R = cfg.run_directory
    if not (R / "lease.json").exists():
        print("resume-teardown: no lease.json: nothing was prepared or armed", file=err)
        return EXIT_INPUTS
    name = f"driver-resume-{time.time_ns()}"
    driver = Driver(cfg, runner, out=out, err=err, evidence_name=name)
    driver.evidence.mkdir(mode=0o700)
    (driver.evidence / "receipts").mkdir(mode=0o700)
    (driver.evidence / "transcripts").mkdir(mode=0o700)
    if delete_after_person_check:
        # A person has confirmed an equal inventory after the timer fired (an unresolved dispatch). Re-check it here.
        audit = R / f"audit-person-{time.time_ns()}"
        audit.mkdir(mode=0o700)
        code, _ = driver.tear("7.4", "independent read-only inventory", cfg.py("infra", "cloud", "rehearsal_lease.py") + [
            "prepare", "--directory", str(audit), "--credential-file", str(cfg.credential_file), "--run",
            secrets.token_hex(16), "--expires", str(int(time.time()) + 900)], 120)
        loaded = []
        for unit in (f"terrace-cloud-{cfg.run}.timer", f"terrace-cloud-{cfg.run}.service"):
            _, output = runner.run([str(SYSTEMD / "systemctl"), "show", unit, "--property=LoadState", "--value"], 15,
                                   label="resume-load")
            loaded.append(output.decode(errors="replace").strip())
        equal = code == 0 and read_json(audit / "lease.json")["baseline"] == read_json(R / "lease.json")["baseline"]
        if not equal or any(state != "not-found" for state in loaded):
            print("resume-teardown: inventory differs or the timer is still loaded; nothing deleted", file=err)
            return EXIT_ESCALATE
        deleted = driver.delete_material()
        lease.write_private(driver.evidence / "closing.json", {"run": cfg.run, "deleted": deleted,
                                                               "personChecked": True})
        print(f"resume-teardown: inventory equals the baseline; {len(deleted)} paths deleted", file=out)
        return EXIT_PASS
    driver.teardown_running = True
    verified = driver.teardown()
    code = EXIT_PASS if verified and driver.local_clean else EXIT_ESCALATE
    pointer = R.parent / (cfg.run + ".open")
    driver.close(code, "resume", verified, pointer)
    return code


# ---- Small subcommands -------------------------------------------------------------------------------------------------

def wait_tcp(address, port, seconds, connect=socket.create_connection, monotonic=time.monotonic, sleep=time.sleep):
    rescue_install.require(re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}", address) is not None, "address rejected")
    start = monotonic()
    while monotonic() - start < seconds:
        try:
            connect((address, port), timeout=5).close()
            return int(monotonic() - start)
        except OSError:
            sleep(3)
    return None


def stage_project(run_directory, source, bun, runner):
    R = private_run_directory(run_directory)
    project = R / "project"
    if os.path.lexists(project):
        raise Refused("RUN/project already exists")
    os.umask(0o077)
    project.mkdir(mode=0o700)
    (project / "rehearsal-create").mkdir(mode=0o700)
    for name in apply.PROJECT_FILES:
        rescue_install.write_new_text(project / "rehearsal-create" / name,
                                      (source / "infra/cloud/rehearsal-create" / name).read_text())
    for name in apply.PARENT_FILES:
        rescue_install.write_new_text(project / name, (source / "infra/cloud" / name).read_text())
    code, _ = runner.run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], 600, label="stage-bun",
                         cwd=str(project))
    if code:
        raise Failed(f"bun install exited {code}")
    expected = apply.fingerprint_source(lambda path: (source / "infra/cloud" / path).read_bytes())
    if apply.source_sha(project / "rehearsal-create") != expected:
        raise Failed("the staged project differs from the source")
    return expected


# ---- CLI -------------------------------------------------------------------------------------------------------------

def add_common(parser):
    parser.add_argument("--run-directory", type=Path, required=True,
                        help="private 0700 directory named by the 32-hex run id, prepared by lease_host_steps.py prepare")
    parser.add_argument("--source", type=Path, default=ROOT, help="the staged `git archive` of the revision")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--credential-file", type=Path, help="default RUN/provider-token (0600, never opened here)")


def add_inputs(parser):
    add_common(parser)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--kexec-archive", type=Path, required=True)
    parser.add_argument("--kexec-archive-sha256", required=True)
    parser.add_argument("--install-outputs", type=Path, help="default RUN/lease (out-link of lease-host.nix install)")
    parser.add_argument("--variants", type=Path, help="default RUN/variants (out-link of lease-host.nix variants)")
    parser.add_argument("--ssh-cidr", required=True, help="the build machine's public IPv4 as a /32")
    parser.add_argument("--unit-path", required=True, help="PATH for the bounded writer units (pulumi and node)")
    parser.add_argument("--expect-baseline", required=True, help="provider inventory before the lease, KIND=N,...")
    for name in ("ssh-keygen", "nix", "nix-store"):
        parser.add_argument("--" + name, default=name)
    for name in ("rclone", "restic", "sops", "terrace-secrets"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--curl")
    parser.add_argument("--ssh")
    parser.add_argument("--min-free-gib", type=int, default=10)
    parser.add_argument("--queue-idle-command", help="optional: an executable that exits 0 when the heavy queue is idle")


def config_from(args):
    return Config(run_directory=args.run_directory, source=args.source, python=args.python,
                  credential_file=args.credential_file, revision=args.revision,
                  manifest_sha256=args.source_manifest_sha256, ssh_keygen=args.ssh_keygen, nix=args.nix,
                  nix_store=args.nix_store, kexec_archive=args.kexec_archive, kexec_sha256=args.kexec_archive_sha256,
                  install_outputs=args.install_outputs, variants=args.variants, ssh_cidr=args.ssh_cidr,
                  unit_path=args.unit_path, rclone=args.rclone, restic=args.restic, sops=args.sops,
                  terrace_secrets=args.terrace_secrets, curl=args.curl, ssh=args.ssh,
                  expect_baseline=parse_baseline(args.expect_baseline), min_free_bytes=args.min_free_gib * 1024 ** 3,
                  queue_command=args.queue_idle_command)


def parser():
    top = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = top.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest", help="print the digest of a staged tree")
    manifest.add_argument("tree", type=Path)
    stage = sub.add_parser("stage-project", help="stage the reviewed creator in RUN/project")
    add_common(stage)
    stage.add_argument("--bun", required=True)
    plan = sub.add_parser("plan", help="validate and print the plan; contacts nothing")
    add_inputs(plan)
    plan.add_argument("--start", help="planned start, UTC, YYYY-MM-DDTHH:MM (default now)")
    run = sub.add_parser("run", help="run the approved plan, then teardown")
    add_inputs(run)
    run.add_argument("--approved-plan-sha", required=True)
    resume = sub.add_parser("resume-teardown", help="the recovery sequence for a lost runner")
    add_common(resume)
    resume.add_argument("--delete-after-person-check", action="store_true",
                        help="after an unresolved dispatch: re-check the inventory and that the timer has gone, "
                             "then delete the run material")
    wait = sub.add_parser("wait-tcp", help=argparse.SUPPRESS)
    wait.add_argument("address")
    wait.add_argument("--port", type=int, default=22)
    wait.add_argument("--seconds", type=int, default=300)
    return top


def main(argv=None, runner=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    runner = runner or Runner()
    try:
        if args.command == "manifest":
            print(tree_manifest(args.tree))
            return EXIT_PASS
        if args.command == "wait-tcp":
            seconds = wait_tcp(args.address, args.port, args.seconds)
            print(f"TCP {args.port} {'answered after ' + str(seconds) + ' s' if seconds is not None else 'did not answer'}")
            return EXIT_PASS if seconds is not None else EXIT_STEP_FAILED
        if args.command == "stage-project":
            print("Staged creator source", stage_project(args.run_directory, args.source, args.bun, runner))
            return EXIT_PASS
        if args.command == "resume-teardown":
            cfg = Config(run_directory=private_run_directory(args.run_directory), source=args.source,
                         python=args.python, credential_file=args.credential_file)
            return resume_teardown(cfg, runner, delete_after_person_check=args.delete_after_person_check)
        cfg = config_from(args)
        if args.command == "plan":
            start = time.time() if args.start is None else datetime.strptime(args.start, "%Y-%m-%dT%H:%M").replace(
                tzinfo=timezone.utc).timestamp()
            plan = make_plan(cfg, runner, start)
            path = cfg.run_directory / "lease-plan.json"
            if path.exists():
                path.unlink()
            lease.write_private(path, plan)
            digest = sha256_file(path)
            print(render_plan(plan, digest), end="")
            return EXIT_PASS
        return Driver(cfg, runner).run(args.approved_plan_sha)
    except (Refused, Failed, lease.Rejected) as refusal:
        print(f"lease driver: refused: {refusal}", file=sys.stderr)
        return EXIT_INPUTS


if __name__ == "__main__":
    sys.exit(main())
