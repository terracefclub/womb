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
  resume-teardown ...           the recovery sequence for a lost runner: cleanup, inventory, final audit, disarm
  wait-tcp ADDRESS              (internal) wait for TCP 22 after the installed boot; no SSH, nothing trusted

Guards (docs/cloud-rehearsal.md, "Production lease driver"): stop at the first non-zero exit or failed guard, no
retry, straight to teardown (G1); the provider token only ever by file path and never opened here (G2); no SSH
without a pin (G3, in the tools); a kexec archive only from this revision's `infra/nixos/kexec-installer.nix` (G5);
no build inside the lease (G6); no overlap with 03:30 UTC +-15 min or 22:25-03:10 UTC (G7); only run-scoped
resources, through the tools' own selection (G8).

Exit codes: 0 pass; 1 a step failed and every clean-up was verified; 2 inputs refused before anything was armed;
3 ESCALATE: some clean-up (provider or local) is not verified, a person must act, recovery commands are printed;
4 interrupted and every clean-up verified. The code is computed from the state, never from whether a receipt could
be written.
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
import select
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
UNIT_WAIT = 240             # teardown waits this long for a running bounded writer unit (RuntimeMaxSec=230)
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
SYSTEMCTL = str(SYSTEMD / "systemctl")
PYTHON_FLAGS = ("-E", "-s", "-B")  # not -I: it implies -P, which drops the tool's own directory from sys.path
EXIT_PASS, EXIT_STEP_FAILED, EXIT_INPUTS, EXIT_ESCALATE, EXIT_INTERRUPTED = 0, 1, 2, 3, 4
EXIT_TEXT = {0: "pass", 1: "a step failed; every clean-up verified", 2: "inputs refused; nothing armed",
             3: "ESCALATE: a clean-up is not verified", 4: "interrupted; every clean-up verified"}
HOST_EXIT_MEANING = {0: "pass", 1: "a host step failed; its clean-up verified", 2: "inputs refused; nothing contacted",
                     3: "host-steps clean-up NOT verified: escalate", 4: "interrupted; its clean-up verified"}
HOST_VERIFIED = (0, 1, 2, 4)  # any other host-steps status, a death by signal or no status at all is unverified
OUTPUT_LIMIT = 256 * 1024
PREP_FILES = ("host-operator-ed25519", "host-operator-ed25519.pub", "deploy-key.sec", "deploy-key.pub",
              "spare-key.sec", "spare-key.pub", "escrow.age", "run-ca.pem", "sftp-client.pub", "lease-inputs.json",
              "lease-inputs.nix")
PREP_DIRECTORIES = ("secrets", "host-stage", "sftp")
FRESH_NAMES = ("lease.json", "closed.json", "lease-closed.json", "operator-ed25519", "rehearsal_lease.py",
               "foundation-input.json", "original-input.json", "host-known-hosts", "host-steps", "host-evidence",
               "driver", "preflight", "original-app-bootstrap-nonce")
TOKEN = "provider-token"
# Key, escrow, deploy-key, store, CA and Pulumi secret material: not needed by cleanup, the timer or resume-teardown.
SECRET_FILES = ("operator-ed25519", "operator-ed25519.pub", "host-operator-ed25519", "host-operator-ed25519.pub",
                "deploy-key.sec", "deploy-key.pub", "spare-key.sec", "spare-key.pub", "escrow.age", "run-ca.pem",
                "sftp-client.pub", "original-app-bootstrap-nonce", "original-app-installed-ed25519",
                "foundation/passphrase.json", "original/passphrase.json", "foundation/plan.json", "original/plan.json")
SECRET_DIRECTORIES = ("secrets", "host-stage", "foundation/backend", "original/backend", "foundation/pulumi-home",
                      "original/pulumi-home")
# The SFTP endpoint's directory and the host steps' scratch: kept while their local clean-up is not verified.
LOCAL_DIRECTORIES = ("sftp", "host-steps")
# What an armed timer and resume-teardown need: never deleted while the provider side is not verified.
RECOVERY_KEEP = (TOKEN, "rehearsal_lease.py", "lease.json", "lock", "*-created.json", "*-dispatched.json",
                 "*-action-*.json", "*-owned-*.json", "original-input.json", "foundation-input.json")


class Refused(ValueError):
    pass


class Failed(RuntimeError):
    pass


def running_as_root():
    return os.geteuid() == 0


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


def resolve_tool(path, store_dir):
    """An executable given by absolute path, resolved once: its real path, and its sha256 unless it is in the store."""
    if not path or not Path(path).is_absolute():
        raise Refused(f"tool {path!r} must be an absolute path")
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise Refused(f"tool {path} is not an executable file")
    store = os.path.realpath(store_dir) + "/"
    if real.startswith(store):
        return {"path": str(path), "real": real}
    return {"path": str(path), "real": real, "sha256": sha256_file(real)}


# ---- Configuration ---------------------------------------------------------------------------------------------------

@dataclass
class Config:
    run_directory: Path
    source: Path = ROOT
    python: str = sys.executable
    credential_file: Path = None
    revision: str = ""
    manifest_sha256: str = ""
    ssh_keygen: str = ""
    nix: str = ""
    nix_store: str = ""
    sudo: str = "/run/wrappers/bin/sudo"
    kexec_archive: Path = None
    kexec_sha256: str = ""
    install_outputs: Path = None
    variants: Path = None
    ssh_cidr: str = ""
    unit_path: str = ""
    child_path: str = ""
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
    bound_scale: float = 1.0  # tests shrink every bound; the plan records the real ones

    def __post_init__(self):
        self.run_directory = Path(self.run_directory)
        self.source = Path(self.source)
        if self.credential_file is None:
            self.credential_file = self.run_directory / TOKEN
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
        return [self.python, *PYTHON_FLAGS, self.tool(*parts)]

    def bound(self, seconds):
        return max(0.05, seconds * self.bound_scale)

    def child_env(self):
        """The whole environment of every child: nothing inherited beyond these names."""
        home = pwd.getpwuid(os.getuid()).pw_dir
        return {"PATH": self.child_path or "/run/wrappers/bin:" + str(SYSTEMD), "HOME": home, "USER": self.user,
                "LOGNAME": self.user, "LANG": "C.UTF-8", "TZ": "UTC"}


def parse_baseline(text):
    counts = {kind: 0 for kind in lease.INVENTORY}
    for item in filter(None, (text or "").split(",")):
        kind, _, number = item.partition("=")
        if kind not in counts or not number.isdigit():
            raise Refused("--expect-baseline is KIND=N,... over the provider inventory kinds")
        counts[kind] = int(number)
    return counts


# ---- Subprocess runner -----------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    code: int        # the child's own exit status (negative: killed by that signal), None if it never ended
    output: bytes
    reason: str = None   # None, "bound" or "interrupted": the driver stopped it
    gone: bool = True    # the child and its whole process group are verified gone


Outcome.__iter__ = lambda self: iter((self.code, self.output))  # `code, output = runner.run(...)`
Outcome.__getitem__ = lambda self, index: (self.code, self.output)[index]


def outcome_of(value):
    return value if isinstance(value, Outcome) else Outcome(*value)


class Runner:
    """Runs a child in its own process group, reading its output with a cap while it runs. Signals never interrupt it:
    the driver's handler sets a flag that `interrupted` reads, and the runner then stops the child itself."""

    def __init__(self):
        self.current = None
        self.lock = threading.Lock()
        self.leftovers = []

    def run(self, argv, timeout, *, label, foreground=True, grace=CHILD_GRACE, cwd=None, env=None,
            interrupted=None):
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, cwd=cwd, env=env)
        if foreground:
            with self.lock:
                self.current = process
        chunks, size, reason = [], 0, None
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        deadline = time.monotonic() + timeout
        open_pipe = True
        try:
            while True:
                if open_pipe:
                    ready, _, _ = select.select([descriptor], [], [], 0.1)
                    if ready:
                        open_pipe, size = self.read(descriptor, chunks, size)
                else:
                    time.sleep(0.1)
                if process.poll() is not None:
                    break
                if interrupted is not None and interrupted():
                    reason = "interrupted"
                    break
                if time.monotonic() >= deadline:
                    reason = "bound"
                    break
            gone = self.stop(process, grace)
            if open_pipe:
                for _ in range(64):  # what is already in the pipe; a descendant elsewhere may still hold it
                    ready, _, _ = select.select([descriptor], [], [], 0)
                    if not ready:
                        break
                    open_pipe, size = self.read(descriptor, chunks, size)
                    if not open_pipe:
                        break
        finally:
            process.stdout.close()
            if foreground:
                with self.lock:
                    self.current = None
        if not gone:
            self.leftovers.append(process)
        output = b"".join(chunks)
        if reason == "bound":
            output += b"\n(bound reached; the driver stopped the child)\n"
        return Outcome(process.returncode, output, reason, gone)

    @staticmethod
    def read(descriptor, chunks, size):
        try:
            data = os.read(descriptor, 65536)
        except BlockingIOError:
            return True, size
        if not data:
            return False, size
        if size < OUTPUT_LIMIT:
            chunks.append(data[:OUTPUT_LIMIT - size])
        return True, size + len(data)

    @staticmethod
    def group_alive(pgid):
        """True, False, or None when it cannot be told (a member we may not signal, such as sudo)."""
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return None

    def stop(self, process, grace):
        """Stop the child and every member of its group. True only when the whole group is verified gone."""
        pgid = process.pid
        if process.poll() is None:
            for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                except OSError:
                    return False  # cannot signal it: cannot verify
                try:
                    process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.poll() is None:
                return False
        # The child is gone; descendants left in its group (the pipe holders) are stopped too.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            alive = self.group_alive(pgid)
            if alive is None:
                return False
            if not alive:
                return True
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.5)
        return self.group_alive(pgid) is False

    def abort_current(self):
        with self.lock:
            process = self.current
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass  # the foreground step then ends at its bound or by the interrupt flag

    def settle(self, seconds):
        """Wait for children the runner could not verify gone (for example a sudo client); True when all are gone."""
        deadline = time.monotonic() + seconds
        while self.leftovers and time.monotonic() < deadline:
            self.leftovers = [p for p in self.leftovers if p.poll() is None or self.group_alive(p.pid) is not False]
            if self.leftovers:
                time.sleep(1)
        return not self.leftovers


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
    return [cfg.sudo, "-n", str(SYSTEMD / "systemd-run"), "--quiet", "--collect", "--wait", "--pipe",
            "--unit=" + unit, "--uid=" + cfg.user, "--service-type=exec", "--property=KillMode=control-group",
            "--property=RuntimeMaxSec=230", "--property=TimeoutStopSec=10s", "--property=UMask=0077",
            "--property=NoNewPrivileges=yes", "--setenv=PATH=" + cfg.unit_path, *argv]


def unit_names(run):
    prefix = f"terrace-cloud-{run}"
    return [f"{prefix}-foundation-preview.service", f"{prefix}-foundation-apply.service",
            f"{prefix}-original-preview.service", f"{prefix}-original-apply.service",
            f"{prefix}-original-app-apply.service"]


def timer_units(run):
    return [f"terrace-cloud-{run}.timer", f"terrace-cloud-{run}.service"]


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
             expected="original-app-bootstrap-nonce (rescue_install.write_proof_nonce), foundation-input.json "
                      "(run key, SSH source /32), rehearsal_lease.py copy for the timer",
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
             grace=HOST_GRACE,
             stop="exit 1/2/4: teardown; exit 3, death by signal or no status: teardown, then escalate"),
    ]


TEARDOWN = (
    ("7.1", f"let a running bounded writer unit finish (up to {UNIT_WAIT} s), then stop the run's five units by exact "
            "name; verify each reads `inactive` or `failed` (anything else, or a failed read, is unknown)",
     "sudo -n systemctl stop <the 5 exact terrace-cloud-RUN-* units>"),
    ("7.2", "stop the throwaway SFTP endpoint if its receipt exists and it was not stopped",
     "rehearsal_sftp.py stop --directory RUN/sftp"),
    ("7.3", "cleanup: delete run-owned resources, three absence reads, baseline preserved",
     "rehearsal_lease.py cleanup --directory RUN --credential-file TOKEN"),
    ("7.4", "independent read-only inventory equals the lease baseline",
     "rehearsal_lease.py prepare --directory RUN/audit-<ns> --credential-file TOKEN --run <fresh> --expires <now+900>"),
    ("7.5", "final audit before disarm: closed.json, the absence receipt (3 reads, baseline preserved, no unresolved "
            "dispatch), inventory equal, writer units inactive", "(driver)"),
    ("7.6", "disarm the expiry timer (only after 7.3, 7.4 and 7.5), then read the timer and its service inactive",
     "rehearsal_timer.py disarm --directory RUN --credential-file TOKEN"),
    ("7.7", "delete by exact path inside RUN: key, escrow, deploy-key, store, CA and Pulumi secret material always "
            "once nothing more needs them; the token copy once the provider side is verified; sftp/ and host-steps/ "
            "once the local clean-up is verified", "(driver)"),
    ("7.8", "builder: no unit of ours remains, no child left; closing receipt; open-lease pointer removed", "(driver)"),
)


# ---- Plan ------------------------------------------------------------------------------------------------------------

def display(cfg, step):
    if step.internal:
        return "(driver) " + step.title + ": " + step.expected
    argv = list(step.command)
    if step.unit:
        argv = bounded_unit(cfg, step.unit, argv)
    return shlex.join(argv)


def check_inputs(cfg, runner, *, at_run=False):
    """Every refusal raises Refused. Returns (inputs, checks) for the plan. Contacts nothing outside this machine."""
    checks = []

    def ok(name, detail):
        checks.append({"check": name, "result": "pass", "detail": detail})

    if running_as_root():
        raise Refused("refusing to run as root: run as the operator account (the bounded units drop to it)")
    R = private_run_directory(cfg.run_directory)
    for name in FRESH_NAMES:
        if os.path.lexists(R / name):
            raise Refused(f"{name} already exists: a lease that stops early is finished; use a new run directory")
    ok("run directory", "private, named by the run id, no earlier lease material")
    root = R.parent
    for entry in root.iterdir():
        if entry.name.endswith(".open") and entry.name != R.name + ".open":
            raise Refused(f"another lease is open ({entry.name})")
        if entry != R and entry.is_dir() and (entry / "lease.json").exists() and not (entry / "lease-closed.json").exists():
            raise Refused(f"another lease directory is not closed ({entry.name})")
    if os.path.lexists(root / (R.name + ".open")) and not at_run:
        raise Refused("this run's own open pointer exists: it already started")
    ok("open leases", "no other open lease pointer or unclosed lease directory beside this one")
    token = cfg.credential_file
    try:
        info = token.lstat()
    except OSError:
        raise Refused("credential file missing") from None
    if (token.parent != R or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or not 16 <= info.st_size <= 4096):
        raise Refused("the credential file must be a 0600 copy directly inside the run directory (G2)")
    ok("credential copy", "0600 file inside the run directory; never opened by the driver")
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
    # S8: every executable resolved once; real paths (and digests outside the store) are part of the plan digest.
    tools = {}
    for name in ("python", "ssh_keygen", "nix", "nix_store", "sudo", "rclone", "restic", "sops", "terrace_secrets",
                 "curl", "ssh", "queue_command"):
        value = getattr(cfg, name)
        if value is None and name in ("curl", "ssh", "queue_command"):
            continue
        tools[name] = resolve_tool(value, cfg.store_dir)
    for part in (cfg.child_path or "").split(":"):
        if not part or not Path(part).is_absolute() or not Path(part).is_dir():
            raise Refused("--child-path must be absolute, existing directories (the children's whole PATH)")
    if not cfg.unit_path or not all(Path(part).is_absolute() for part in cfg.unit_path.split(":")):
        raise Refused("--unit-path must be absolute directories (pulumi and node for the bounded units)")
    ok("tools", f"{len(tools)} executables resolved; children get PATH={cfg.child_path} and nothing else inherited")
    env = cfg.child_env()
    code, output = outcome_of(runner.run(cfg.py("infra", "nixos", "lease_host_steps.py") + ["list"], 60,
                                         label="plan-host-steps-list", env=env))[:2]
    if code:
        raise Refused("lease_host_steps.py list failed in the staged tree")
    host_steps = [line.rstrip() for line in output.decode(errors="replace").splitlines() if line.strip()][:200]
    source_sha = apply.fingerprint_source(lambda path: (cfg.source / "infra/cloud" / path).read_bytes())
    try:
        staged = apply.source_sha(R / "project/rehearsal-create")
    except (OSError, ValueError):
        raise Refused("RUN/project is not staged: run stage-project") from None
    if staged != source_sha:
        raise Refused("RUN/project differs from the staged tree's creator")
    ok("creator project", f"source {source_sha}")
    archive = Path(os.path.realpath(cfg.kexec_archive)) if cfg.kexec_archive else None
    if archive is None or "diagnostic" in str(cfg.kexec_archive) + str(archive):
        raise Refused("G5: kexec archive refused (diagnostic or missing)")
    code, output = runner.run([cfg.nix, "eval", "--raw", "--offline", "--impure", "--file",
                               cfg.tool("infra", "nixos", "kexec-installer.nix"), "archive.outPath"],
                              600, label="plan-kexec-eval", env=env)
    expected = output.decode(errors="replace").strip()
    if code or not rescue_install.STORE.fullmatch(expected):
        raise Refused("G5: could not evaluate infra/nixos/kexec-installer.nix at the staged revision")
    store = Path(os.path.realpath(cfg.store_dir))
    if archive != store / Path(expected).name / "nixos-kexec-installer-x86_64-linux.tar.gz":
        raise Refused("G5: the kexec archive is not this revision's infra/nixos/kexec-installer.nix output")
    if not re.fullmatch(r"[a-f0-9]{64}", cfg.kexec_sha256) or sha256_file(archive) != cfg.kexec_sha256:
        raise Refused("G5: kexec archive digest differs")
    ok("kexec archive (G5)", f"{expected} sha256 {cfg.kexec_sha256}")
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
    code, _ = runner.run([cfg.nix_store, "--check-validity", *paths], 120, label="plan-store-validity", env=env)
    if code:
        raise Refused("G6: a prestaged store path is missing (a build would be needed inside the lease)")
    for link in (cfg.install_outputs, cfg.variants):
        code, output = runner.run([cfg.nix_store, "--query", "--roots", os.path.realpath(link)], 120,
                                  label="plan-store-roots", env=env)
        if code or not any(line.startswith(f"{link} -> ") for line in output.decode(errors="replace").splitlines()):
            raise Refused(f"G6: {Path(link).name} is not a GC root inside the run directory")
    ok("prestaged closures (G6)", f"{len(paths)} store paths valid, rooted by RUN/lease and RUN/variants")
    if not re.fullmatch(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}/32", cfg.ssh_cidr or ""):
        raise Refused("--ssh-cidr must be the build machine's IPv4 /32 (G4)")
    if set(cfg.expect_baseline) != set(lease.INVENTORY):
        raise Refused("--expect-baseline missing")
    free = shutil.disk_usage(R).free
    if free < cfg.min_free_bytes:
        raise Refused(f"only {free} bytes free under the run directory; need {cfg.min_free_bytes}")
    checks.append({"check": "free space", "result": "pass", "detail": f"{free // 1024 ** 3} GiB free"})
    checks.extend(idle_checks(cfg, runner))
    inputs = {"run": cfg.run, "revision": cfg.revision, "manifestSha256": cfg.manifest_sha256,
              "sourceSha256": source_sha, "kexecArchive": str(archive), "kexecSha256": cfg.kexec_sha256,
              "install": {k: outputs[k] for k in ("system", "disko", "installer", "device")}, "variants": variants,
              "sshCidr": cfg.ssh_cidr, "expectBaseline": cfg.expect_baseline, "serverType": SERVER_TYPE,
              "location": LOCATION, "image": IMAGE, "leaseSeconds": LEASE_SECONDS, "tools": tools,
              "childEnvironment": {k: v for k, v in env.items()}, "unitPath": cfg.unit_path, "hostSteps": host_steps,
              "queueCommand": cfg.queue_command}
    return inputs, checks


def idle_checks(cfg, runner):
    checks = []
    for scope in ([], ["--user"]):
        code, output = runner.run([SYSTEMCTL, *scope, "list-units", "--plain",
                                   "--no-legend", "terrace-cloud-*", "terrace-lease-sftp-*"], 30, label="idle-units",
                                  env=cfg.child_env())
        if code or output.strip():
            raise Refused(f"builder not idle: {'user' if scope else 'system'} units of a lease are loaded "
                          "(or systemctl could not tell)")
    checks.append({"check": "builder units", "result": "pass", "detail": "no terrace-cloud-*/terrace-lease-sftp-* unit"})
    if cfg.queue_command:
        code, _ = runner.run([cfg.queue_command], 60, label="idle-queue", env=cfg.child_env())
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


ROOT_STATEMENT = ("Runs as root, through `sudo -n` (NOPASSWD needed for exactly these): (1) `systemd-run` of the five "
                  "bounded writer units, which drop to the invoking user, RuntimeMaxSec=230, NoNewPrivileges; (2) "
                  "`systemctl stop` of those five exact unit names; (3) rehearsal_timer.py's own fixed calls: "
                  "`systemd-run` of the expiry timer (User=invoking user) and `systemctl stop` of that timer and its "
                  "service. rehearsal_escrow.py's sudo calls are not used. No sudo argument comes from an input file.")


def render_plan(plan, digest):
    inputs = plan["inputs"]
    baseline = ", ".join(f"{k}={v}" for k, v in inputs["expectBaseline"].items() if v) or "empty"
    lines = [f"LEASE 6 PLAN for run {inputs['run']} at revision {inputs['revision']}",
             f"Server: {SERVER_TYPE} in {LOCATION}, {IMAGE} then kexec, one IPv4, firewall TCP 22 from "
             f"{inputs['sshCidr']} only",
             f"Expected provider baseline (must equal the current inventory): {baseline}; every other kind 0",
             f"Window (G7): start {plan['window']['plannedStart']}, worst case {WORST_CASE_SECONDS} s, end "
             f"{plan['window']['plannedEnd']}; expiry about {plan['window']['expiresAt']} (now+{LEASE_SECONDS} at arming)",
             f"Cost: expected about {COST['expectedUsd']} USD, cap {COST['capUsd']} USD. {COST['worstCase']}.",
             ROOT_STATEMENT,
             "Exit codes: " + "; ".join(f"{k} {v}" for k, v in EXIT_TEXT.items()),
             f"Install (V0): system {inputs['install']['system']}", f"  disko {inputs['install']['disko']}",
             f"  installer {inputs['install']['installer']}, disk {inputs['install']['device']}",
             "Variants: " + ", ".join(f"{k} {v}" for k, v in inputs["variants"].items()),
             f"Kexec archive: {inputs['kexecArchive']} sha256 {inputs['kexecSha256']}",
             "Tools (resolved now; a different binary at run time is refused):"]
    lines += [f"  {name}: {t['real']}" + (f" sha256 {t['sha256']}" if "sha256" in t else " (store)")
              for name, t in inputs["tools"].items()]
    lines.append(f"Children's whole environment: {inputs['childEnvironment']}; bounded units PATH {inputs['unitPath']}")
    lines.append("Checks:")
    lines += [f"  [{c['result']}] {c['check']}: {c['detail']}" for c in plan["checks"]]
    lines.append("Steps (stop at the first failure, no retry, then teardown). {name} values are filled at run time "
                 "from the earlier step named in the guard:")
    for step in plan["steps"]:
        lines += [f"  {step['id']}  {step['title']}  (bound {step['boundSeconds']} s)",
                  f"       command:  {step['command']}", f"       guard:    {step['guard']}",
                  f"       expected: {step['expected']}", f"       evidence: {step['evidence']}",
                  f"       stop:     {step['stop']}"]
        if step["id"] == "2.1-6":
            lines.append("       host steps (lease_host_steps.py list, staged tree):")
            lines += [f"         {line}" for line in inputs["hostSteps"]]
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
        self.signals = []
        self.teardown_running = False
        self.host_exit = None
        self.host_verified = True
        self.arm_dispatched = False
        self.problems = []            # every local clean-up that could not be verified, in words
        self.state = {"provider": "not armed", "timer": "never armed", "deleted": [], "kept": [],
                      "deleteFailures": []}
        self.audit_dir = None

    # Evidence: best-effort everywhere; state drives behaviour, never a write.
    def log(self, line):
        try:
            with open(self.evidence / "steps.log", "a") as stream:
                stream.write(line + "\n")
        except OSError:
            pass

    def receipt(self, name, data):
        try:
            path = self.evidence / "receipts" / f"{name}.json"
            if path.exists():
                path = self.evidence / "receipts" / f"{name}-{time.time_ns()}.json"
            lease.write_private(path, data)
        except (OSError, ValueError, TypeError):
            self.log(f"receipt {name} could not be written")

    def transcript(self, name, data):
        try:
            with open(self.evidence / "transcripts" / f"{name}.log", "ab") as stream:
                stream.write(data)
        except OSError:
            pass

    def say(self, text, stream=None):
        try:
            print(text, file=stream or self.out, flush=True)
        except (OSError, ValueError):
            pass  # a lost terminal must never skip teardown

    # Signals only set a flag; nothing raises out of the handler.
    def on_signal(self, signum, _frame):
        self.signals.append(signum)
        if self.teardown_running:
            self.say(f"lease driver: signal {signum} ignored; teardown in progress", self.err)
        elif self.interrupted:
            self.say(f"lease driver: signal {signum} ignored; already stopping", self.err)
        else:
            self.interrupted = True
            self.say(f"lease driver: signal {signum}: stopping the current step, then teardown", self.err)

    def run_child(self, argv, bound, *, label, grace=CHILD_GRACE, foreground=True, interruptible=True):
        result = self.runner.run(argv, self.cfg.bound(bound), label=label, grace=self.cfg.bound(grace),
                                 foreground=foreground,
                                 env=self.cfg.child_env(),
                                 interrupted=(lambda: self.interrupted) if interruptible else None)
        return outcome_of(result)

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
                if step.id == "1.1b":
                    self.arm_dispatched = True
                record["command"] = shlex.join(argv)
                outcome = self.run_child(argv, bound, label=step.id, grace=step.grace)
                record.update(exit=outcome.code, stoppedBy=outcome.reason, childGone=outcome.gone)
                self.transcript(step.id, outcome.output)
                if step.host:
                    self.host_exit = outcome.code
                    record["hostExitMeaning"] = HOST_EXIT_MEANING.get(outcome.code, "no status or killed: unverified")
                    if outcome.code not in HOST_VERIFIED or not outcome.gone:
                        self.host_verified = False
                        self.problems.append(f"host steps ended with {outcome.code} "
                                             f"({record['hostExitMeaning']}); their forwards, second session and "
                                             "SFTP endpoint are not verified stopped")
                elif not outcome.gone:
                    self.problems.append(f"step {step.id}: the child or its process group could not be verified gone")
                if self.refresh_failed:
                    raise Failed("binding refresh failed while the step ran")
                if outcome.reason:
                    raise Failed(f"stopped by the driver ({outcome.reason}), exit {outcome.code}")
                if outcome.code:
                    raise Failed(f"exit {outcome.code}")
                if step.after:
                    record["result"] = getattr(self, step.after)(outcome.output) or {}
        except Failed as failure:
            record.update(status="interrupted" if self.interrupted else "failed", error=str(failure))
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
            if self.refresher.is_alive():
                self.problems.append("the binding refresher did not stop")
            self.refresher = None

    def refresh_loop(self):
        argv = self.cfg.py("infra", "cloud", "rehearsal_apply.py") + [
            "binding", PHASE, "--directory", str(self.R), "--credential-file", str(self.cfg.credential_file),
            "--role", ROLE]
        while not self.refresh_stop.wait(self.cfg.refresh_seconds):
            outcome = self.run_child(argv, 60, label="1.4-refresh", foreground=False, interruptible=False)
            self.refreshes += 1
            if outcome.code != 0:
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
            raise Refused("inputs, tools, steps or teardown changed since the plan was approved")
        return current

    def armed(self):
        return self.arm_dispatched or os.path.lexists(self.R / "lease.json")

    def run(self, approved_sha):
        handlers = {sig: signal.signal(sig, self.on_signal) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            return self.guarded_run(approved_sha)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    def guarded_run(self, approved_sha):
        try:
            plan = self.start(approved_sha)
        except Refused as refusal:
            self.say(f"lease driver: refused: {refusal}", self.err)
            return EXIT_INPUTS
        except Exception as problem:  # noqa: BLE001 - nothing was started: an input problem (S12)
            self.say(f"lease driver: refused: {type(problem).__name__} while checking the inputs", self.err)
            return EXIT_INPUTS
        if self.interrupted:
            self.say("lease driver: interrupted before anything started", self.err)
            return EXIT_INTERRUPTED
        pointer = self.R.parent / (self.cfg.run + ".open")
        failed = None
        crashed = False
        current = None
        try:
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
            for current in run_steps(self.cfg):
                if self.interrupted:
                    break
                self.execute(current)
        except Failed:
            failed = current.id if current else "start"
        except BaseException:  # noqa: BLE001 - a crash still tears down; handlers never raise
            crashed = True
            failed = current.id if current else "start"
        # The one teardown entry. Nothing above can bypass it: signal handlers only set a flag.
        return self.finish(failed, crashed, pointer)

    def finish(self, failed, crashed, pointer):
        self.teardown_running = True
        try:
            verified = self.teardown()
        except BaseException:  # noqa: BLE001 - teardown guards every block; this is the last net
            verified = False
            self.problems.append("teardown raised unexpectedly")
        code = self.exit_code(verified, failed, crashed)
        self.close(code, failed, pointer)
        return code

    def exit_code(self, verified, failed, crashed):
        if not verified or self.problems:
            return EXIT_ESCALATE
        if self.interrupted:
            return EXIT_INTERRUPTED
        if failed or crashed:
            return EXIT_STEP_FAILED
        return EXIT_PASS

    # Teardown (step 7): every sub-step in its own guarded block; state drives behaviour.
    def guard(self, label, action, default=None):
        try:
            return action()
        except BaseException as problem:  # noqa: BLE001
            self.log(f"{label}: {type(problem).__name__}")
            self.say(f"[{label}] error: {type(problem).__name__}", self.err)
            return default

    def tear(self, step_id, title, argv, bound, *, label=None):
        started = self.now()
        outcome = self.guard(step_id, lambda: self.run_child(argv, bound, label=label or step_id,
                                                              interruptible=False),
                             Outcome(None, b"", None, False))
        self.transcript(step_id, outcome.output)
        record = {"step": step_id, "title": title, "command": shlex.join(argv), "exit": outcome.code,
                  "childGone": outcome.gone, "seconds": round(self.now() - started, 1)}
        self.receipt(step_id, record)
        good = outcome.code == 0 and outcome.gone
        self.log(f"{step_id} {'passed' if good else 'failed'} exit {outcome.code} {record['seconds']}s {title}")
        self.say(f"[{step_id}] {'passed' if good else 'FAILED'}: {title}")
        if not outcome.gone:
            self.problems.append(f"{step_id}: its child could not be verified gone")
        return outcome.code if outcome.gone else None

    def unit_state(self, unit, label, prop="ActiveState"):
        """The literal state, or "unknown" when systemctl fails or answers anything unexpected."""
        outcome = self.guard(label, lambda: self.run_child([SYSTEMCTL, "show", unit, f"--property={prop}", "--value"],
                                                           15, label=label, interruptible=False),
                             Outcome(None, b"", None, False))
        value = outcome.output.decode(errors="replace").strip()
        if outcome.code != 0 or not re.fullmatch(r"[a-z-]+", value):
            return "unknown"
        return value

    def units_stopped(self, units, label):
        return all(self.unit_state(unit, label) in ("inactive", "failed") for unit in units)

    def teardown(self):
        self.teardown_running = True
        self.guard("7.0-refresher", self.stop_refresher)
        self.guard("7.0-abort", self.runner.abort_current)
        if not self.armed():
            # Nothing was prepared, armed or created: no provider teardown. Delete what this run holds (S5).
            self.log("teardown: nothing armed or created")
            self.state["provider"] = "nothing armed or created"
            self.local_checks()
            self.delete(SECRET_FILES + SECRET_DIRECTORIES + (TOKEN,)
                        + (LOCAL_DIRECTORIES if not self.problems else ()))
            return True
        self.state["timer"] = "armed (or arming was dispatched)"
        units = unit_names(self.cfg.run)
        deadline = self.now() + self.cfg.bound(UNIT_WAIT)
        while True:  # 7.1: let a running writer finish so its created.json resolves the dispatch
            if self.units_stopped(units, "7.1-wait") or self.now() >= deadline:
                break
            self.guard("7.1-sleep", lambda: self.sleep(5 * self.cfg.bound_scale))
        self.tear("7.1", "stop bounded writer units",
                  [self.cfg.sudo, "-n", SYSTEMCTL, "stop", *units], 60)
        units_ok = self.units_stopped(units, "7.1-show")
        if not units_ok:
            self.say("a bounded writer unit is not verified inactive", self.err)
        settle = getattr(self.runner, "settle", None)
        if settle is not None and not self.guard("7.1-settle", lambda: settle(self.cfg.bound(30)), False):
            self.problems.append("a child the driver started (for example a sudo client) is not verified gone")
        self.guard("7.2", self.stop_sftp)
        if not units_ok:
            self.finish_unverified("a writer unit may still hold the lease lock")
            return False
        creds = ["--directory", str(self.R), "--credential-file", str(self.cfg.credential_file)]
        code = self.tear("7.3", "cleanup with three absence reads",
                         self.cfg.py("infra", "cloud", "rehearsal_lease.py") + ["cleanup", *creds], 420)
        if code != 0:
            self.finish_unverified("cleanup did not verify absence")
            return False
        audit = self.guard("7.4-mkdir", self.make_audit_dir)
        code = None if audit is None else self.tear(
            "7.4", "independent read-only inventory",
            self.cfg.py("infra", "cloud", "rehearsal_lease.py") + [
                "prepare", "--directory", str(audit), "--credential-file", str(self.cfg.credential_file),
                "--run", secrets.token_hex(16), "--expires", str(int(self.now()) + 900)], 120)
        if code != 0 or not self.guard("7.4-compare", self.inventory_equal, False):
            self.say("independent inventory differs from the lease baseline", self.err)
            self.finish_unverified("the independent inventory does not equal the baseline")
            return False
        audit_result = self.guard("7.5", self.final_audit, {"pass": False})
        if not audit_result.get("pass"):
            self.finish_unverified("the final audit failed")
            return False
        code = self.tear("7.6", "disarm the expiry timer",
                         self.cfg.py("infra", "cloud", "rehearsal_timer.py") + ["disarm", *creds], 60)
        if code != 0:
            self.state["timer"] = "unknown: disarm failed"
            self.finish_unverified("disarm failed")
            return False
        if not self.units_stopped(timer_units(self.cfg.run), "7.6-show"):
            self.state["timer"] = "unknown: disarm reported success but the timer could not be read inactive"
            self.finish_unverified("the timer state after disarm is unknown")
            return False
        self.state.update(provider="verified absent, baseline preserved", timer="disarmed")
        self.local_checks()
        if self.problems:
            # (ii) provider verified, local clean-up not: delete the token and secrets, keep sftp/ and host-steps/.
            self.delete(SECRET_FILES + SECRET_DIRECTORIES + (TOKEN,))
            self.state["kept"] = list(LOCAL_DIRECTORIES) + ["driver/ (evidence)"]
        else:
            self.delete(SECRET_FILES + SECRET_DIRECTORIES + (TOKEN,) + LOCAL_DIRECTORIES)
        return True

    def finish_unverified(self, why):
        """(i) provider side not verified: the timer stays armed. Keep what recovery needs, delete the rest."""
        self.state["provider"] = "NOT verified: " + why
        self.local_checks()
        self.delete(SECRET_FILES + SECRET_DIRECTORIES)
        self.state["kept"] = list(RECOVERY_KEEP) + (list(LOCAL_DIRECTORIES) if self.problems else []) + [
            "driver/ (evidence)"]

    def make_audit_dir(self):
        self.audit_dir = self.R / f"audit-{time.time_ns()}"
        self.audit_dir.mkdir(mode=0o700)
        return self.audit_dir

    def stop_sftp(self):
        sftp = self.R / "sftp"
        if (sftp / "sftp-listener.json").exists() and not (sftp / "sftp-stopped.json").exists():
            code = self.tear("7.2", "stop the SFTP endpoint",
                             self.cfg.py("infra", "cloud", "rehearsal_sftp.py") + ["stop", "--directory", str(sftp)], 60)
            if code != 0:
                self.problems.append("the throwaway SFTP endpoint is not verified stopped (`rehearsal_sftp.py status "
                                     f"--directory {sftp}`, then `stop`)")

    def local_checks(self):
        if not self.guard("7.8-units", self.local_units_clear, False):
            self.problems.append("a terrace-cloud-* or terrace-lease-sftp-* unit is still loaded on this machine")

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
        facts["writerUnitsInactive"] = self.units_stopped(unit_names(run), "7.5-show")
        facts["pass"] = all(facts.values())
        try:
            lease.write_private(self.evidence / "final-audit.json", facts)
        except OSError:
            self.log("final-audit.json could not be written")
        self.say(f"[7.5] {'passed' if facts['pass'] else 'FAILED'}: final audit")
        return facts

    def delete(self, names):
        """Attempt every path; record each failure; never stop at the first."""
        for name in names:
            try:
                if remove_inside(self.R, name):
                    self.state["deleted"].append(name)
            except BaseException as problem:  # noqa: BLE001
                self.state["deleteFailures"].append(f"{name}: {type(problem).__name__}")
        if self.state["deleteFailures"]:
            self.problems.append("deletion failed for: " + ", ".join(self.state["deleteFailures"]))
        self.log(f"7.7 deleted {len(self.state['deleted'])} paths: {' '.join(self.state['deleted'])}")

    def local_units_clear(self):
        try:
            idle_checks(Config(run_directory=self.R, source=self.cfg.source, user=self.cfg.user,
                               child_path=self.cfg.child_path), self.runner)
            return True
        except Refused:
            self.say("units of a lease remain loaded on this machine (or systemctl could not tell)", self.err)
            return False

    def close(self, code, failed, pointer):
        summary = {"run": self.cfg.run, "exit": code, "result": EXIT_TEXT[code], "failedStep": failed,
                   "interrupted": self.interrupted, "signals": self.signals, "hostStepsExit": self.host_exit,
                   "hostStepsMeaning": (HOST_EXIT_MEANING.get(self.host_exit, "no status or killed: unverified")
                                        if self.host_exit is not None or not self.host_verified else None),
                   "state": self.state, "problems": self.problems, "bindingRefreshes": self.refreshes}
        if code == EXIT_ESCALATE:
            self.say(recovery_text(self.cfg, summary), self.err)  # before any write (M1)
        self.guard("close-time", lambda: summary.update(closedAt=stamp(self.now())))
        self.guard("closing.json", lambda: lease.write_private(self.evidence / f"closing-{time.time_ns()}.json",
                                                               summary))
        if code != EXIT_ESCALATE:
            closed = self.guard("lease-closed.json", lambda: self.write_closed(summary), False)
            if closed:
                self.guard("pointer", lambda: pointer.unlink(missing_ok=True))
            else:
                self.say(f"lease driver: lease-closed.json could not be written; remove {pointer} by hand once you "
                         "have checked the evidence", self.err)
        self.guard("SHA256SUMS", self.write_sums)
        self.say(f"lease driver: exit {code} ({EXIT_TEXT[code]}); evidence in {self.evidence}",
                 self.err if code == EXIT_ESCALATE else None)

    def write_closed(self, summary):
        path = self.R / "lease-closed.json"
        if path.exists():
            path = self.R / f"lease-closed-{time.time_ns()}.json"
        lease.write_private(path, summary)
        return True

    def write_sums(self):
        sums = [f"{sha256_file(p)}  {p.relative_to(self.evidence)}" for p in sorted(self.evidence.rglob("*"))
                if p.is_file() and p.name != "SHA256SUMS"]
        (self.evidence / "SHA256SUMS").write_text("\n".join(sums) + "\n")


def recovery_text(cfg, summary):
    R, T, run = cfg.run_directory, cfg.credential_file, cfg.run
    py = f"{cfg.python} {' '.join(PYTHON_FLAGS)} {cfg.source}"
    creds = f"--directory {R} --credential-file {T}"
    state = summary.get("state", {})
    provider = state.get("provider", "unknown")
    timer = state.get("timer", "unknown")
    lines = ["=" * 78, "ESCALATE: a clean-up could NOT be verified. A person must act now.",
             f"Run {run}: failed step {summary.get('failedStep')}; host steps exit {summary.get('hostStepsExit')}.",
             f"Provider side: {provider}.", f"Expiry timer terrace-cloud-{run}.timer: {timer}."]
    for problem in summary.get("problems", []):
        lines.append(f"Local: {problem}.")
    lines.append("Deleted: " + (", ".join(state.get("deleted", [])) or "nothing") + ".")
    if state.get("kept"):
        lines.append("Kept on purpose (the armed timer, resume-teardown or diagnosis need them): "
                     + ", ".join(state["kept"]) + ".")
    if state.get("deleteFailures"):
        lines.append("Could not delete: " + ", ".join(state["deleteFailures"]) + ".")
    if not provider.startswith("verified"):
        lines += [
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
            f"equals the baseline, nothing remains: check `systemctl show terrace-cloud-{run}.timer` is not loaded,",
            "then run resume-teardown --delete-after-person-check, which repeats both checks, stops the SFTP",
            "endpoint, deletes the run material and closes the lease.",
            "If the inventory shows a run resource, delete it in the provider console by its exact name",
            f"terrace-r-{run}-..., never anything else, and write down what you did."]
    else:
        lines += ["The provider side is verified and the timer disarmed. Check on this machine, then delete by hand:",
                  f"  systemctl --user list-units 'terrace-lease-sftp-*'; systemctl list-units 'terrace-cloud-*'",
                  f"  {py}/infra/cloud/rehearsal_sftp.py status --directory {R}/sftp (then `stop`)",
                  "  no ssh forward to the leased address remains (ps -u $USER -o pid,args | grep ssh)",
                  f"  then remove {R}/sftp and {R}/host-steps and {R}.open's pointer ({R.parent}/{run}.open)"]
    lines.append("=" * 78)
    return "\n".join(lines)


def resume_teardown(cfg, runner, *, delete_after_person_check=False, out=sys.stdout, err=sys.stderr, now=time.time,
                    sleep=time.sleep):
    R = cfg.run_directory
    if running_as_root():
        print("resume-teardown: refusing to run as root", file=err)
        return EXIT_INPUTS
    if not (R / "lease.json").exists():
        print("resume-teardown: no lease.json: nothing was prepared or armed", file=err)
        return EXIT_INPUTS
    driver = Driver(cfg, runner, out=out, err=err, now=now, sleep=sleep, evidence_name=f"driver-resume-{time.time_ns()}")
    driver.arm_dispatched = True
    pointer = R.parent / (cfg.run + ".open")
    handlers = {sig: signal.signal(sig, driver.on_signal) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    driver.teardown_running = True
    try:
        try:
            for name in ("", "receipts", "transcripts"):
                (driver.evidence / name).mkdir(mode=0o700, exist_ok=True)
        except OSError:
            pass
        if delete_after_person_check:
            verified = driver.guard("person-check", driver.person_checked_close, False)
        else:
            verified = driver.guard("resume", driver.teardown, False)
        code = EXIT_PASS if verified and not driver.problems else EXIT_ESCALATE
        driver.close(code, "resume", pointer)
        return code
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def person_checked_close(self):
    """After an unresolved dispatch and the timer's own run: verify, stop the endpoint, delete, close (S9)."""
    self.state["timer"] = "expected gone after firing at expiry"
    self.guard("7.2", self.stop_sftp)
    if not self.units_stopped(unit_names(self.cfg.run), "resume-units"):
        self.finish_unverified("a writer unit is not verified inactive")
        return False
    audit = self.guard("7.4-mkdir", self.make_audit_dir)
    code = None if audit is None else self.tear(
        "7.4", "independent read-only inventory", self.cfg.py("infra", "cloud", "rehearsal_lease.py") + [
            "prepare", "--directory", str(audit), "--credential-file", str(self.cfg.credential_file), "--run",
            secrets.token_hex(16), "--expires", str(int(self.now()) + 900)], 120)
    equal = code == 0 and self.guard("7.4-compare", self.inventory_equal, False)
    loaded = [self.unit_state(unit, "resume-load", "LoadState") for unit in timer_units(self.cfg.run)]
    if not equal or any(state != "not-found" for state in loaded):
        self.say("resume-teardown: inventory differs or the timer is still loaded (or unknown); nothing deleted",
                 self.err)
        self.finish_unverified("the person check did not hold: inventory or timer")
        return False
    self.state.update(provider="verified absent by the independent inventory after the timer fired",
                      timer="gone (not loaded)")
    self.local_checks()
    self.delete(SECRET_FILES + SECRET_DIRECTORIES + (TOKEN,) + (LOCAL_DIRECTORIES if not self.problems else ()))
    return True


Driver.person_checked_close = person_checked_close


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
    if running_as_root():
        raise Refused("refusing to run as root")
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
    code, _ = outcome_of(runner.run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], 600, label="stage-bun",
                                    cwd=str(project)))[:2]
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
    parser.add_argument("--python", default=sys.executable, help="absolute path of the Python for every tool")
    parser.add_argument("--credential-file", type=Path, help="default RUN/provider-token (0600, never opened here)")
    parser.add_argument("--sudo", default="/run/wrappers/bin/sudo")
    parser.add_argument("--child-path", default="/run/wrappers/bin:/run/current-system/sw/bin",
                        help="the whole PATH every child gets (absolute directories)")


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
    for name in ("ssh-keygen", "nix", "nix-store", "rclone", "restic", "sops", "terrace-secrets"):
        parser.add_argument("--" + name, required=True, help="absolute path")
    parser.add_argument("--curl")
    parser.add_argument("--ssh")
    parser.add_argument("--min-free-gib", type=int, default=10)
    parser.add_argument("--queue-idle-command", help="optional: an executable that exits 0 when the heavy queue is idle")


def config_from(args):
    return Config(run_directory=args.run_directory, source=args.source, python=args.python,
                  credential_file=args.credential_file, revision=args.revision,
                  manifest_sha256=args.source_manifest_sha256, ssh_keygen=args.ssh_keygen, nix=args.nix,
                  nix_store=args.nix_store, sudo=args.sudo, kexec_archive=args.kexec_archive,
                  kexec_sha256=args.kexec_archive_sha256, install_outputs=args.install_outputs,
                  variants=args.variants, ssh_cidr=args.ssh_cidr, unit_path=args.unit_path,
                  child_path=args.child_path, rclone=args.rclone, restic=args.restic, sops=args.sops,
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
                             "stop the SFTP endpoint, then delete the run material and close the lease")
    wait = sub.add_parser("wait-tcp", help=argparse.SUPPRESS)
    wait.add_argument("address")
    wait.add_argument("--port", type=int, default=22)
    wait.add_argument("--seconds", type=int, default=300)
    return top


def main(argv=None, runner=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    runner = runner or Runner()
    if args.command == "run":
        cfg = None
        try:
            cfg = config_from(args)
            return Driver(cfg, runner).run(args.approved_plan_sha)
        except BaseException as problem:  # noqa: BLE001 - M1: after arming, never exit 2 or a bare traceback
            run_dir = Path(args.run_directory)
            if (run_dir / "lease.json").exists() or (run_dir.parent / (run_dir.name + ".open")).exists():
                print(f"lease driver: unexpected {type(problem).__name__} after the run started", file=sys.stderr)
                if cfg is not None:
                    print(recovery_text(cfg, {"failedStep": "driver error", "state": {
                        "provider": "unknown", "timer": "unknown (possibly armed)"}}), file=sys.stderr)
                return EXIT_ESCALATE
            print(f"lease driver: refused: {type(problem).__name__} before anything started", file=sys.stderr)
            return EXIT_INPUTS
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
                         python=args.python, credential_file=args.credential_file, sudo=args.sudo,
                         child_path=args.child_path)
            return resume_teardown(cfg, runner, delete_after_person_check=args.delete_after_person_check)
        cfg = config_from(args)
        start = time.time() if args.start is None else datetime.strptime(args.start, "%Y-%m-%dT%H:%M").replace(
            tzinfo=timezone.utc).timestamp()
        plan = make_plan(cfg, runner, start)
        path = cfg.run_directory / "lease-plan.json"
        if path.exists():
            path.unlink()
        lease.write_private(path, plan)
        print(render_plan(plan, sha256_file(path)), end="")
        return EXIT_PASS
    except (Refused, Failed, lease.Rejected) as refusal:
        print(f"lease driver: refused: {refusal}", file=sys.stderr)
        return EXIT_INPUTS
    except Exception as problem:  # noqa: BLE001 - S12
        print(f"lease driver: refused: {type(problem).__name__}", file=sys.stderr)
        return EXIT_INPUTS


if __name__ == "__main__":
    sys.exit(main())
