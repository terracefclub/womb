"""Run the production lease host's run-sheet steps 2.1 to 6 against one installed host over pinned SSH.

The same program serves the VM rehearsal (`test_lease_host_vm.py`) and the lease itself: the lease wrapper adds only
the provider's create and teardown around it. It carries out one step at a time, each with its guard, its expected
observation and its evidence, exactly as the run sheet in docs/cloud-rehearsal.md ("Host steps") lists them, and stops
at the first non-zero exit or failed guard with no retry (G1). Then it runs only the host-independent clean-up it owns:
the SSH forwards it opened, the throwaway SFTP endpoint it started and its own scratch directory, each by exact
process, unit or path. It never deletes the run directory or anything the caller made.

Subcommands:

  list                     the steps, their guards, expected observations and evidence (no inputs, no host)
  prepare RUN ...          step 0.6: the run material in RUN (keys, secrets store, run CA, SFTP endpoint, build inputs)
  pin RUN --address --port the pinned known-hosts line for the installed host, from the secrets store's host key
  run RUN ...              steps 2.1 to 6 (or a selection) against the host; --dry-run validates and lists only

Nothing here prints a private key, a secret value or the provider token. SSH is always pinned: StrictHostKeyChecking
yes, no global known hosts, BatchMode, IdentitiesOnly, ed25519 host keys only, never trust on first use (G3).
"""

import argparse
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]

# ---- The run directory (docs/cloud-rehearsal.md, "Host steps driver") ----------------------------------------------
BOOTSTRAP_KEY = "operator-ed25519"        # the bootstrap root key of the lease tooling; the installed host must refuse it
OPERATOR_KEY = "host-operator-ed25519"    # the installed operator key (terrace.production.operatorKeys)
KNOWN_HOSTS = "host-known-hosts"          # one line pinning the installed host key at --address/--port
DEPLOY_KEY = "deploy-key.sec"             # Nix secret signing key, the build machine's working key
SPARE_KEY = "spare-key.sec"               # Nix secret signing key, the offline spare
ESCROW = "escrow.age"                     # the run's throwaway escrow identity (SOPS_AGE_KEY_FILE)
RUN_CA = "run-ca.pem"                     # the run CA certificate (public)
STORE = "secrets"                         # TERRACE_SECRETS_STORE
STAGE = "host-stage"                      # `terrace-secrets host create --stage`; removed by step 3.2
SFTP = "sftp"                             # infra/cloud/rehearsal_sftp.py --directory (prepared by `prepare`)
SFTP_CLIENT = "sftp-client.pub"           # public half of backup/primary/sftp-key
INPUTS_JSON = "lease-inputs.json"         # public build inputs and run facts, written by `prepare`
INPUTS_NIX = "lease-inputs.nix"           # the same, as lease-host.nix arguments
WORK = "host-steps"                       # the driver's own scratch (removed by its clean-up)
HOST = "app"
VARIANTS = ("v0", "v1", "release", "v2", "v3", "v4", "v5")
TIMERS = ("terrace-backup.timer", "terrace-backup-check.timer", "terrace-cms-sweep.timer")
PREVIOUS_RELEASE = ROOT / "platform/production/deploy-previous-release.json"
NEXT_RELEASE = ROOT / "platform/website-release.json"
SFTP_TOOL = ROOT / "infra/cloud/rehearsal_sftp.py"
DEPLOY_TOOL = ROOT / "platform/production/terrace-deploy.sh"
ISSUER = "https://identity.terrace.invalid/realms/terrace"
NAMES = ("web.terrace.invalid", "cms.terrace.invalid", "identity.terrace.invalid")
STORE_PATH = re.compile(r"/nix/store/[0-9abcdfghijklmnpqrsvwxyz]{32}-[A-Za-z0-9+_.?=-]+")
ED25519 = re.compile(r"ssh-ed25519 ([A-Za-z0-9+/]{68})")
BOOT_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
NIX_SECRET = re.compile(r"([A-Za-z0-9._-]+):[A-Za-z0-9+/]{86}==")
# Validity of the run CA and its leaf. The production expiry check (`terrace-tls check`, terrace.production.tls.minDays
# in platform/production/tls.nix) fails a certificate with fewer than 14 days left; dev run 5 (2026-11-27) failed step 5.2 on a 7-day leaf.
TLS_CHECK_DAYS = 14
LEAF_DAYS = 30
# Seconds between the refusal probes of step 2.1 (sshd's PerSourcePenalties: see step_2_1).
PROBE_PAUSE = 7
OUTPUT_LIMIT = 1024 * 1024
TERM_GRACE = 15
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy")
# The shortest secret value the redactor matches. Every generated value of the store is far longer (hex64, an
# OpenSSH key, a PEM key); a shorter one could match ordinary text, so it is not loaded (and is reported).
MIN_SECRET_LENGTH = 16
SFTP_SECONDS = (300, 14400)
PUBLIC_STORE_VALUES = ("tls/certificate",)
SYSTEMCTL = "/run/current-system/sw/bin/systemctl"
TRANSCRIPT_LIMIT = 64 * 1024

EXIT_PASS, EXIT_STEP_FAILED, EXIT_INPUTS, EXIT_CLEANUP, EXIT_INTERRUPTED = 0, 1, 2, 3, 4


class Refused(Exception):
    """Inputs refused before any step ran (exit 2)."""


class Failed(Exception):
    """A step's command exited non-zero or its guard or expectation failed (G1: no retry)."""


class Interrupted(Exception):
    pass


class Leak(Failed):
    """Output contained private key material or a secret value: never written anywhere."""


def stop_group(process, grace=TERM_GRACE):
    """Stop a child's process group: SIGTERM, up to GRACE seconds for its own clean-up (the SFTP helper stops its
    units on SIGTERM), then SIGKILL. True when the child has exited."""
    if process.poll() is not None:
        return True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


# ---- Steps --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    id: str
    title: str
    guard: str
    expected: str
    evidence: str


STEPS = (
    Step("2.1", "first-boot window",
         "the known-hosts file pins the secrets store's host key (G3)",
         "operator SSH works with the pinned key; a wrong pin, root, password authentication and the bootstrap key "
         "are refused; not adopted; writers and timers inactive; sops-install-secrets not succeeded; only TCP 22 "
         "listens; TCP 443 at the host's address does not answer; UEFI, systemd-boot, an address from DHCP",
         "transcript; boot ID, listeners, units, bootctl, df, closure size"),
    Step("3.1", "throwaway SFTP endpoint and reverse forward",
         "the SFTP directory is prepared and never started",
         "the endpoint listens on the build machine's loopback; the forward binds the host's 127.0.0.1:<backup port> "
         "only and answers with the endpoint's SSH banner",
         "the endpoint's receipt, the forward's command and process"),
    Step("3.2", "secrets push and unstage",
         "the staged host key exists and the host has no secrets yet",
         "`Installed <n> encrypted secrets.`, `Host state: not adopted`; verify PASS; sops-install-secrets succeeded; "
         "the staging directory is gone",
         "push and unstage output"),
    Step("3.3", "adopt --new",
         "the forward answers (adopt --new reads the backup repository)",
         "`Adopted: the writers start now and at every boot.`; every writer active (the CMS and website skipped by "
         "their migration condition) and every writer timer active",
         "adopt output, unit and timer states"),
    Step("3.4", "migrate, start, first backup, restic check off the host",
         "adopted; the forward answers",
         "the marker and ledger are the installed release's; CMS and website active; repository initialised; one "
         "snapshot of host app; `restic check --read-data` passes on the build machine",
         "migration and backup seconds, ledger, snapshot list, check output"),
    Step("3.5", "HTTPS through an SSH local forward with the run CA",
         "the local forward binds the build machine's loopback only",
         "OIDC discovery with issuer https://identity.terrace.invalid/realms/terrace; the CMS and the website answer "
         "200, verified against the run CA",
         "HTTP status lines, issuer"),
    Step("4.1", "reboot and return",
         "no busy unit and no queued job",
         "back with the same pinned key and a new boot ID; booted and current system V0; adopted; writers and timers "
         "active; no failed unit",
         "seconds to TCP 22 and to SSH, unit states"),
    Step("5.1", "V1: plain switch and rollback (deploy key)",
         "no writer timer elapses within 10 minutes; deploy path on with two keys",
         "dry run exit 0 (the release does not change); after the quiesce exit 0 again; activate records `success`; "
         "the marker file is V1's; rollback records `success` and runs V0; status has no WARNING",
         "copy seconds and size, the two records, status before and after"),
    Step("5.2", "release change with its migration (deploy key)",
         "no writer timer elapses within 10 minutes; the pre-update backup is taken first",
         "dry run with the site up exits 3 (NOT READY); backup success, two snapshots; dry run exit 0; activate "
         "records `switched; down until the CMS migration`; the migration writes the new marker and ledger, a second "
         "run applies nothing; CMS and website active; checks pass; timers re-armed",
         "migration seconds, ledger before and after, records"),
    Step("5.3", "V2: initrd change, --boot and a reboot (signed by the spare key only)",
         "the toplevel carries the spare key's signature and not the working key's",
         "dry run and activate without --boot refused (`initrd differ(s)`), nothing changed; --boot sets the boot "
         "default; status warns; after the reboot /run/booted-system is V2",
         "signatures, refusal text, reboot seconds, ESP use"),
    Step("5.4", "V3: sshd change activated from an SSH session with a second session open",
         "a second session is open and printing",
         "the switch finishes (`success`); recorded: whether the activating session dropped, whether the second "
         "session and the reverse forward survived, whether sshd restarted, whether the same key reconnects",
         "both sessions' exit codes and output, sshd PIDs, the switch journal"),
    Step("5.5", "V4 failing unit, then V5 failing boot loader with the automatic revert",
         "V4 and V5 differ from the running system only by their change",
         "V4: `failed (exit 4)` naming terrace-lease-broken.service, no revert (it runs), rollback to V3; V5: "
         "`failed (exit 1)`, revert {from V5, to V3, result success}, bootDefaultAfter V3, status without WARNING, "
         "and a reboot boots V3",
         "records, client output, reboot"),
    Step("6", "final evidence",
         "none",
         "generations, ESP and disk use, journal errors, the deployments log",
         "final receipt; the timing ledger"),
)
STEP_IDS = tuple(step.id for step in STEPS)


def select_steps(only=None, start=None, end=None):
    ids = list(STEP_IDS)
    if only:
        chosen = [item.strip() for item in only.split(",") if item.strip()]
        if not chosen or any(item not in ids for item in chosen) or len(set(chosen)) != len(chosen):
            raise Refused("unknown or repeated step in --steps")
        return [step for step in STEPS if step.id in chosen]
    if start and start not in ids or end and end not in ids:
        raise Refused("--from/--to name unknown steps")
    first = ids.index(start) if start else 0
    last = ids.index(end) if end else len(ids) - 1
    if first > last:
        raise Refused("--from/--to give an empty range")
    return list(STEPS[first:last + 1])


def listing(steps=STEPS):
    lines = []
    for step in steps:
        lines.append(f"{step.id:4} {step.title}")
        lines.append(f"     guard:    {step.guard}")
        lines.append(f"     expected: {step.expected}")
        lines.append(f"     evidence: {step.evidence}")
    return "\n".join(lines) + "\n"


# ---- Files --------------------------------------------------------------------------------------------------------

def private_directory(path):
    path = Path(path).resolve()
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused(f"{path.name}: not a private (0700, own) directory")
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(path)):
        raise Refused("unsupported characters in the run directory path")
    return path


def private_file(path, limit=65536):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 \
            or info.st_nlink != 1 or not 1 <= info.st_size <= limit:
        raise Refused(f"{Path(path).name}: not a private (0600, own, single link) file")
    return Path(path).read_bytes()


def write_new(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data if isinstance(data, bytes) else data.encode())


def remove_file(path):
    """Overwrite, then unlink one regular file by its exact path; a link is unlinked, never followed."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode):
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"\0" * info.st_size)
            stream.flush()
            os.fsync(stream.fileno())
    os.unlink(path)


def store_host_key(run):
    line = (run / STORE / "hosts" / HOST / "ssh_host_ed25519_key.pub").read_text()
    match = ED25519.match(line)
    if not match:
        raise Refused("the secrets store holds no ed25519 host key for app")
    return match[1]


def known_hosts_name(address, port):
    return address if port == 22 else f"[{address}]:{port}"


def pin_line(run, address, port):
    return f"{known_hosts_name(address, port)} ssh-ed25519 {store_host_key(run)}\n"


def checked_address(address, port):
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError:
        raise Refused("--address must be a dotted IPv4 address") from None
    if not 1 <= port <= 65535:
        raise Refused("--port out of range")
    return str(parsed)


def read_variants(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or set(data) != set(VARIANTS) \
            or not all(isinstance(value, str) and STORE_PATH.fullmatch(value) for value in data.values()) \
            or len(set(data.values())) != len(VARIANTS):
        raise Refused("the variants file is not lease-host.nix's `variants` output (v0, v1, release, v2 to v5)")
    return data


def nix_key_name(path):
    match = NIX_SECRET.fullmatch(private_file(path, 256).decode("ascii").strip())
    if not match:
        raise Refused(f"{Path(path).name} is not a Nix secret signing key")
    return match[1]


# ---- Redaction (fail closed) -------------------------------------------------------------------------------------

class Redactor:
    """Refuses any output that carries private key material or a known secret value.

    A refusal names the rule that matched (a marker, or the source of a known value) and never the value."""

    MARKERS = ((b"PRIVATE KEY-----", "a PEM or OpenSSH private key marker"),
               (b"AGE-SECRET-KEY-1", "an age secret key marker"))

    def __init__(self):
        self.forbidden = {}

    def add_file(self, path):
        try:
            data = Path(path).read_bytes()
        except OSError:
            return
        self.add_bytes(data, f"a line of {Path(path).name}")

    def add_bytes(self, data, source="a known secret value"):
        lines = [line.strip() for line in data.splitlines()]
        body = lines[1:-1] if len(lines) > 2 and lines[0].startswith(b"-----BEGIN") else lines
        for line in body:
            if len(line) >= MIN_SECRET_LENGTH:
                self.forbidden.setdefault(line, source)
        match = re.search(rb":([A-Za-z0-9+/]{86}==)", data)
        if match:
            self.forbidden.setdefault(match[1], f"the secret half of {source}")

    def add_store(self, tree, prefix=""):
        """Every value of a decrypted store (its sops metadata and the public TLS certificate left out). Values shorter
        than MIN_SECRET_LENGTH are not loaded and are named (none of the generated ones is that short)."""
        loaded, short = 0, []
        for key, value in tree.items():
            if not prefix and key == "sops":
                continue
            name = prefix + key
            if isinstance(value, dict):
                inner = self.add_store(value, name + "/")
                loaded += inner["loaded"]
                short += inner["short"]
            elif isinstance(value, str) and name not in PUBLIC_STORE_VALUES:
                if len(value.strip()) < MIN_SECRET_LENGTH:
                    short.append(name)
                else:
                    self.add_bytes(value.encode(), f"the store value {name}")
                    loaded += 1
        return {"loaded": loaded, "short": short}

    def rule(self, data):
        """The first rule DATA breaks, or None."""
        for marker, name in self.MARKERS:
            if marker in data:
                return name
        for item, source in self.forbidden.items():
            if item in data:
                return source
        return None

    def check(self, data, where="output"):
        rule = self.rule(data)
        if rule:
            raise Leak(f"{where} carried {rule}; nothing of it was written")
        return data


# ---- Evidence -----------------------------------------------------------------------------------------------------

class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        if os.path.lexists(self.directory):
            raise Refused("the evidence directory must not exist yet")
        self.directory.mkdir(mode=0o700, parents=False)
        for name in ("transcripts", "receipts"):
            (self.directory / name).mkdir(mode=0o700)
        self.ledger = []
        self.current = None

    def log(self, line):
        with open(self.directory / "steps.log", "a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n")

    def open_step(self, step):
        self.current = open(self.directory / "transcripts" / f"{step.id}.log", "a", encoding="utf-8")
        self.write(f"# step {step.id}: {step.title}\n# guard: {step.guard}\n# expected: {step.expected}\n"
                   f"# evidence: {step.evidence}\n")

    def close_step(self):
        if self.current:
            self.current.close()
            self.current = None

    def write(self, text):
        if self.current:
            self.current.write(text)
            self.current.flush()

    def receipt(self, name, data):
        write_new(self.directory / "receipts" / f"{name}.json", json.dumps(data, indent=1, sort_keys=True) + "\n")

    def finish(self, summary):
        write_new(self.directory / "ledger.json", json.dumps(self.ledger, indent=1) + "\n")
        write_new(self.directory / "summary.json", json.dumps(summary, indent=1, sort_keys=True) + "\n")
        lines = []
        for path in sorted(p for p in self.directory.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(self.directory)}\n")
        write_new(self.directory / "SHA256SUMS", "".join(lines))


@dataclass
class Result:
    code: int
    out: str
    err: str
    seconds: float


# ---- The driver ---------------------------------------------------------------------------------------------------

@dataclass
class Tools:
    rclone: str
    restic: str
    sops: str
    terrace_secrets: str
    curl: str = "curl"
    ssh: str = "ssh"
    nix: str = "nix"
    python: str = sys.executable


@dataclass
class Driver:
    run: Path
    address: str
    port: int
    evidence: Evidence
    tools: Tools
    variants: dict
    inputs: dict
    sftp_seconds: int = 14400
    redactor: Redactor = field(default_factory=Redactor)
    processes: dict = field(default_factory=dict)
    obs: dict = field(default_factory=dict)
    sftp_attempted: bool = False
    cleanup_failures: list = field(default_factory=list)
    interrupted_step: str = None
    timings: dict = field(default_factory=dict)

    # -- low level --
    @property
    def work(self):
        return self.run / WORK

    def env(self, extra=None, without=()):
        env = {key: value for key, value in os.environ.items()
               if key not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID") and key.lower() not in without}
        env["LC_ALL"] = "C"
        # The SFTP helper talks to the user's systemd manager (`systemctl --user`, `systemd-run --user`), which needs
        # XDG_RUNTIME_DIR. The lease driver gives its children only PATH, HOME, USER, LOGNAME, LANG and TZ.
        runtime = f"/run/user/{os.geteuid()}"
        if "XDG_RUNTIME_DIR" not in env and os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = runtime
        env.update(extra or {})
        return env

    def execute(self, argv, timeout, *, stdin=None, env=None):
        """Run one local command, bounded in time and output; returns (code, stdout, stderr) as bytes."""
        start = time.monotonic()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                       stdout=out, stderr=err, env=env or self.env(), start_new_session=True)
            try:
                if stdin is not None:
                    process.stdin.write(stdin)
                    process.stdin.close()
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = 124
            finally:
                stop_group(process)
            out.seek(0)
            err.seek(0)
            return code, out.read(OUTPUT_LIMIT), err.read(OUTPUT_LIMIT), time.monotonic() - start

    def command(self, argv, timeout, *, shown=None, stdin=None, env=None, ok=(0,), quiet=False):
        code, out, err, seconds = self.execute(argv, timeout, stdin=stdin, env=env)
        label = shown or shlex.join(argv[:3])
        for name, data in (("stdout", out), ("stderr", err)):
            rule = self.redactor.rule(data)
            if rule:
                # The command and the rule, never the value.
                self.evidence.write(f"\n$ {label}\n# exit {code} after {seconds:.1f} s\n"
                                    f"# REFUSED: its {name} carried {rule}; nothing of it was written\n")
                raise Leak(f"the {name} of `{label}` carried {rule}; nothing of it was written")
        result = Result(code, out.decode("utf-8", "replace").replace("\r\n", "\n"),
                        err.decode("utf-8", "replace").replace("\r\n", "\n"), seconds)
        self.evidence.write(f"\n$ {shown or shlex.join(argv)}\n# exit {code} after {seconds:.1f} s\n")
        if not quiet:
            for name, text in (("stdout", result.out), ("stderr", result.err)):
                if text:
                    clipped = text[:TRANSCRIPT_LIMIT]
                    self.evidence.write(f"--- {name}\n{clipped}{'' if clipped.endswith(chr(10)) else chr(10)}")
                    if len(text) > TRANSCRIPT_LIMIT:
                        self.evidence.write(f"--- ({len(text) - TRANSCRIPT_LIMIT} more characters not kept)\n")
        if ok is not None and code not in ok:
            raise Failed(f"exit {code}: {shown or shlex.join(argv[:3])}")
        return result

    def pipe(self, argv, timeout, *, env=None):
        """Like execute, but standard output and error through pipes into memory (no temporary file)."""
        start = time.monotonic()
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env or self.env(), start_new_session=True)
        try:
            out, err = process.communicate(timeout=timeout)
            code = process.returncode
        except subprocess.TimeoutExpired:
            out, err, code = b"", b"", 124
        finally:
            stop_group(process)
        return code, out[:OUTPUT_LIMIT], err[:OUTPUT_LIMIT], time.monotonic() - start

    def read_secret(self, argv, timeout, *, shown, env=None):
        """A command whose standard output IS a secret the driver needs (the store's values for the redactor, the
        repository password for the off-host check). The output is read through a pipe into this process's memory,
        never into a file, the transcript or a receipt, and is not matched against the known values (it is one of
        them); its standard error is checked like any other output. Only `restic_offhost` puts one value on disk:
        RUN/host-steps/repository-password (0600, in a 0700 directory), for the length of the restic commands, then
        overwritten and removed, and its absence is verified again by the clean-up."""
        code, out, err, seconds = self.pipe(argv, timeout, env=env)
        self.evidence.write(f"\n$ {shown}\n# exit {code} after {seconds:.1f} s (standard output is a secret: not kept)\n")
        rule = self.redactor.rule(err)
        if rule:
            self.evidence.write(f"# REFUSED: its stderr carried {rule}; nothing of it was written\n")
            raise Leak(f"the stderr of `{shown}` carried {rule}; nothing of it was written")
        if code != 0:
            raise Failed(f"exit {code}: {shown}")
        return out.decode("utf-8", "replace")

    def note(self, text):
        self.evidence.write(f"# {text}\n")

    def expect(self, condition, message):
        if not condition:
            self.evidence.write(f"# GUARD FAILED: {message}\n")
            raise Failed(message)
        self.evidence.write(f"# ok: {message}\n")

    def ssh_options(self, *, identity=None, known_hosts=None):
        return ["-F", "/dev/null", "-i", str(identity or self.run / OPERATOR_KEY), "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts or self.run / KNOWN_HOSTS}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519",
                "-o", "UpdateHostKeys=no", "-o", "ControlMaster=no", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", "-p", str(self.port)]

    def ssh_argv(self, remote, *, user="operator", identity=None, known_hosts=None, tty=False, extra=()):
        return [self.tools.ssh, *self.ssh_options(identity=identity, known_hosts=known_hosts),
                *(["-tt"] if tty else []), *extra, f"{user}@{self.address}", *([remote] if remote else [])]

    def ssh(self, remote, timeout=120, *, ok=(0,), user="operator", identity=None, known_hosts=None, tty=False,
            shown=None, stdin=None, quiet=False):
        argv = self.ssh_argv(remote, user=user, identity=identity, known_hosts=known_hosts, tty=tty)
        label = f"ssh {'-tt ' if tty else ''}{user}@host {shown or remote}"
        return self.command(argv, timeout, shown=label, ok=ok, stdin=stdin, quiet=quiet)

    def root(self, script, timeout=120, *, ok=(0,), tty=False, shown=None, quiet=False):
        """A root script on the host (operator's passwordless sudo, as every documented command)."""
        remote = shlex.join(["sudo", "-n", "bash", "-c", "set -o pipefail; " + script])
        return self.ssh(remote, timeout, ok=ok, tty=tty, shown=shown or f"sudo: {script}", quiet=quiet)

    def secrets_env(self):
        # `check` and `push` compare the store's escrow recipient with the one pinned in the host parameters.
        return self.env({"TERRACE_SECRETS_STORE": str(self.run / STORE), "SOPS_AGE_KEY_FILE": str(self.run / ESCROW),
                         "TERRACE_ESCROW_RECIPIENT": self.inputs["escrowRecipient"]})

    # -- background SSH processes (the forwards and the second session) --
    def start_background(self, name, argv, output=None):
        if name in self.processes and self.processes[name][0].poll() is None:
            raise Failed(f"{name} is already running")
        sink = open(output, "wb") if output else subprocess.DEVNULL
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.DEVNULL,
                                   env=self.env(), start_new_session=True)
        if output:
            sink.close()
        self.processes[name] = (process, output)
        self.note(f"started {name} (pid {process.pid}): {shlex.join(argv[-4:])}")
        return process

    def stop_background(self, name):
        entry = self.processes.pop(name, None)
        if not entry:
            return True
        process = entry[0]
        stop_group(process, grace=10)
        gone = process.poll() is not None and not Path(f"/proc/{process.pid}").exists()
        self.note(f"stopped {name} (pid {process.pid}); gone: {gone}")
        return gone

    def alive(self, name):
        entry = self.processes.get(name)
        return bool(entry) and entry[0].poll() is None

    def free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    # -- host facts --
    def boot_id(self):
        value = self.ssh("cat /proc/sys/kernel/random/boot_id", 30).out.strip()
        self.expect(BOOT_ID.fullmatch(value), "the host reports a boot ID")
        return value

    def systems(self):
        out = self.ssh("readlink -f /run/current-system /run/booted-system /nix/var/nix/profiles/system", 30).out.split()
        self.expect(len(out) == 3, "current, booted and boot-default systems read")
        return dict(zip(("current", "booted", "bootDefault"), out))

    def wanted_units(self):
        out = self.ssh("systemctl show terrace-production.target -p Wants --value", 30).out.split()
        self.expect(bool(out) and all(re.fullmatch(r"[A-Za-z0-9@_.:-]+", unit) for unit in out),
                    "terrace-production.target wants units")
        return sorted(out)

    def unit_states(self, units):
        props = self.ssh(shlex.join(["systemctl", "show", "-p", "Id", "-p", "ActiveState", "-p", "Result",
                                     "-p", "ConditionResult", *units]), 60).out
        states = {}
        for block in props.strip().split("\n\n"):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if "Id" in fields:
                states[fields["Id"]] = {key: fields.get(key, "") for key in ("ActiveState", "Result", "ConditionResult")}
        return states

    def failed_units(self):
        return self.ssh("systemctl list-units --failed --plain --no-legend --full", 30).out.split("\n")

    def records(self, count=2):
        out = self.root(f"tail -n {int(count)} /var/lib/terrace-production/deployments.log", 30).out
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    STATUS_FIRST = re.compile(r"(adopted [0-9T:-]+Z|not adopted: no writer starts at boot|adoption as [a-z]+ not finished)")

    def status(self):
        """`terrace-production status`: exit 0 and its first line, always one of three, or the step fails (an empty or
        failed read must never pass an absence check such as no_warning)."""
        result = self.root("terrace-production status", 60, ok=(0,))
        first = (result.out.splitlines() or [""])[0]
        self.expect(self.STATUS_FIRST.fullmatch(first), f"terrace-production status answered (first line: {first!r})")
        self.expect("deploy path: " in result.out, "terrace-production status printed its deploy path line")
        return result.out

    def generation_of(self, system):
        out = self.root('for l in /nix/var/nix/profiles/system-*-link; do echo "$l $(readlink -f "$l")"; done',
                        30, quiet=True).out
        numbers = [int(re.search(r"system-([0-9]+)-link", line)[1]) for line in out.splitlines() if line.split()[1:] == [system]]
        return max(numbers) if numbers else None

    def settled(self, *, cms_running=True):
        """Writers active (the CMS and website skipped only before their first migration), timers active, none failed."""
        states = self.unit_states(self.wanted_units())
        for unit, state in states.items():
            skipped = unit in ("terrace-cms.service", "terrace-web.service") and not cms_running \
                and state["ActiveState"] == "inactive" and state["ConditionResult"] == "no"
            self.expect(state["ActiveState"] == "active" or skipped, f"{unit} active ({state['ActiveState']})")
        self.expect(self.failed_units() == [""], "no failed unit")
        return states

    def timers_clear(self, minutes=10):
        out = self.ssh(shlex.join(["systemctl", "show", "-p", "Id", "-p", "NextElapseUSecRealtime", "--timestamp=unix",
                                   *TIMERS]), 30).out
        now = time.time()
        blocks = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                  for block in out.strip().split("\n\n")]
        self.expect(sorted(fields.get("Id", "") for fields in blocks) == sorted(TIMERS),
                    "systemctl show answered for each writer timer")
        for fields in blocks:
            value = fields.get("NextElapseUSecRealtime", "")
            if not value:
                continue  # a stopped timer elapses never
            match = re.fullmatch(r"@([0-9]+)", value)
            self.expect(match, f"{fields.get('Id')}: next elapse readable")
            self.expect(not 0 <= int(match[1]) - now <= minutes * 60,
                        f"{fields.get('Id')}: does not elapse within {minutes} minutes")

    def no_busy(self):
        out = self.ssh(shlex.join(["systemctl", "show", "-p", "ActiveState", "terrace-switch.service",
                                   "terrace-backup.service", "terrace-backup-check.service",
                                   "terrace-cms-migrate.service", "terrace-cms-sweep.service"]), 30).out
        lines = out.split()
        self.expect(len(lines) == 5 and all(line in ("ActiveState=inactive", "ActiveState=failed") for line in lines),
                    "no switch, backup, check, migration or sweep is running")
        self.expect(self.ssh("systemctl list-jobs --no-legend", 30).out.strip() == "", "no queued systemd job")

    # -- the SFTP endpoint and the reverse forward --
    def sftp_receipt(self):
        return json.loads(private_file(self.run / SFTP / "sftp-listener.json"))

    def forward_answers(self, port):
        probe = ("exec 3<>/dev/tcp/127.0.0.1/" + str(int(port)) + " && head -c 8 <&3")
        result = self.ssh(shlex.join(["timeout", "10", "bash", "-c", probe]), 30, ok=None)
        return result.code == 0 and result.out.startswith("SSH-2.0")

    def open_forward(self):
        backup_port = int(self.inputs["backupPort"])
        option = self.command([self.tools.python, "-B", str(SFTP_TOOL), "forward", "--directory", str(self.run / SFTP),
                               "--host-port", str(backup_port)], 30).out.split()
        self.expect(len(option) == 2 and option[0] == "-R"
                    and re.fullmatch(rf"127\.0\.0\.1:{backup_port}:127\.0\.0\.1:[0-9]{{4,5}}", option[1]),
                    "the helper's forward binds the host's loopback and ends at the endpoint's loopback")
        argv = self.ssh_argv(None, extra=["-N", "-o", "ExitOnForwardFailure=yes", *option])
        self.start_background("reverse-forward", argv)
        deadline = time.monotonic() + 30
        while not self.forward_answers(backup_port):
            self.expect(self.alive("reverse-forward") and time.monotonic() < deadline,
                        "the reverse forward came up within 30 s")
            time.sleep(2)
        listen = self.ssh(f"ss -Htln 'sport = :{backup_port}'", 30).out.split()
        bound = [word for word in listen if word.endswith(f":{backup_port}")]
        self.expect(bound and all(word in (f"127.0.0.1:{backup_port}", f"[::1]:{backup_port}") for word in bound),
                    f"the host binds the forward on loopback only ({' '.join(bound)})")

    def require_forward(self):
        """The reverse forward must still answer: a forward that died where nothing explains it fails the step (no
        silent repair)."""
        backup_port = int(self.inputs["backupPort"])
        self.expect(self.alive("reverse-forward") and self.forward_answers(backup_port),
                    "the reverse forward opened in step 3.1 still answers")

    def reopen_forward(self, reason):
        """After a loss the step explains (a reboot ends every SSH connection; an sshd restart may): open the forward
        again and record why. Returns the observation for the receipt."""
        backup_port = int(self.inputs["backupPort"])
        if self.alive("reverse-forward") and self.forward_answers(backup_port):
            return {"reopened": False, "reason": reason}
        self.stop_background("reverse-forward")
        self.open_forward()
        self.note(f"reverse forward reopened: {reason}")
        return {"reopened": True, "reason": reason}

    # -- reboots --
    def reboot(self, label):
        self.no_busy()
        before = self.boot_id()
        self.stop_background("reverse-forward")
        start = time.monotonic()
        # The connection can end before ssh sees the command's exit: 0 or 255.
        self.root("systemctl reboot", 60, ok=(0, 255))
        tcp = None
        deadline = start + 900
        while True:
            self.expect(time.monotonic() < deadline, f"{label}: the host came back within 900 s")
            time.sleep(5)
            try:
                with socket.create_connection((self.address, self.port), timeout=3) as conn:
                    conn.settimeout(5)
                    if not conn.recv(8).startswith(b"SSH-2.0"):
                        continue
            except OSError:
                continue
            tcp = tcp or time.monotonic() - start
            result = self.ssh("cat /proc/sys/kernel/random/boot_id", 30, ok=None, quiet=True)
            if result.code == 0 and BOOT_ID.fullmatch(result.out.strip()) and result.out.strip() != before:
                break
        ssh_seconds = time.monotonic() - start
        state = self.ssh("timeout 600 systemctl is-system-running --wait", 660, ok=None).out.strip()
        up = time.monotonic() - start
        self.expect(state == "running", f"{label}: the system reached running ({state})")
        facts = {"bootIdBefore": before, "bootIdAfter": self.boot_id(), "secondsToTcp22": round(tcp or 0, 1),
                 "secondsToSsh": round(ssh_seconds, 1), "secondsToRunning": round(up, 1)}
        self.timings[label] = facts
        return facts

    # -- the deploy path --
    def deploy(self, name, key_file):
        target = self.variants[name]
        address = self.address if self.port == 22 else f"{self.address}:{self.port}"
        size = self.command([self.tools.nix, "--extra-experimental-features", "nix-command", "path-info", "-S", target],
                            120).out.split()
        result = self.command(["bash", str(DEPLOY_TOOL), "--key", str(self.run / key_file), "--address", address,
                               "--identity", str(self.run / OPERATOR_KEY), "--known-hosts", str(self.run / KNOWN_HOSTS),
                               target], 3600, env=self.env({"PATH": self.path_with_ssh()}))
        sigs = self.ssh(shlex.join(["nix", "--extra-experimental-features", "nix-command", "path-info", "--sigs", target]),
                        60).out
        carried = signed_by(sigs, target, {key: nix_key_name(self.run / key) for key in (DEPLOY_KEY, SPARE_KEY)})
        self.timings[f"copy-{name}"] = {"seconds": round(result.seconds, 1), "closureBytes": int(size[-1])}
        return {"target": target, "copySeconds": round(result.seconds, 1), "closureBytes": int(size[-1]),
                "signedBy": carried}

    def path_with_ssh(self):
        return os.pathsep.join([str(Path(self.tools.ssh).parent)] * (os.sep in self.tools.ssh)
                               + [os.environ.get("PATH", "")])

    def activate(self, *args, ok=(0,), timeout=1200):
        return self.root(shlex.join(["terrace-production", *args]), timeout, ok=ok, tty=True)

    def quiesce(self):
        self.root("systemctl stop " + " ".join(TIMERS), 60)
        self.root("systemctl stop terrace-cms-sweep.service terrace-web.service terrace-cms.service", 300)

    def stop_timers_again(self):
        # H10: an activation or rollback starts again the timers step 6 stopped (docs/host-updates.md step 7).
        self.root("systemctl stop " + " ".join(TIMERS), 60)

    def restart_after_quiesce(self):
        self.root("systemctl reset-failed terrace-cms.service terrace-web.service", 60)
        self.root("systemctl start terrace-cms.service terrace-web.service", 600)
        self.root("systemctl start " + " ".join(TIMERS), 60)

    def no_warning(self):
        text = self.status()
        self.expect("WARNING" not in text, "terrace-production status prints no WARNING")
        return text

    def finished(self, records):
        last = records[-1]
        self.expect(last.get("phase") == "finished", "the last record is a finished record")
        return last

    def ledger(self):
        return self.root("runuser -u postgres -- psql -XAtw -v ON_ERROR_STOP=1 -d terrace_cms "
                         "-c 'select name from payload_migrations order by id'", 60).out.split()

    def marker(self):
        return self.root("cat /var/lib/terrace-cms/migrated-revision", 30).out.strip()

    def https(self, label):
        local = self.free_port()
        argv = self.ssh_argv(None, extra=["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{local}:127.0.0.1:443"])
        self.start_background("https-forward", argv)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    socket.create_connection(("127.0.0.1", local), timeout=2).close()
                    break
                except OSError:
                    self.expect(self.alive("https-forward") and time.monotonic() < deadline, "the local forward came up")
                    time.sleep(1)
            facts = {}
            for name, path in (("identity", "/realms/terrace/.well-known/openid-configuration"),
                               ("cms", "/api/events?limit=1"), ("web", "/events/")):
                host = f"{name}.terrace.invalid"
                # -q first: no ~/.curlrc; no proxy for the loopback forward, and none in the environment.
                result = self.command([self.tools.curl, "-q", "--noproxy", "*", "--silent", "--show-error", "--max-time", "30",
                                       "--cacert", str(self.run / RUN_CA), "--resolve", f"{host}:{local}:127.0.0.1",
                                       "--output", "-", "--write-out", "\n%{http_code}", f"https://{host}:{local}{path}"],
                                      60, ok=None, quiet=True, env=self.env(without=PROXY_VARIABLES))
                body, _, code = result.out.rpartition("\n")
                self.note(f"{label}: https://{host}{path} -> {code} ({len(body)} bytes)")
                facts[name] = code
                self.expect(result.code == 0 and code == "200", f"{host}{path} answers 200 over the run CA")
                if name == "identity":
                    issuer = json.loads(body).get("issuer")
                    self.expect(issuer == ISSUER, f"OIDC issuer {issuer}")
                    facts["issuer"] = issuer
            return facts
        finally:
            self.stop_background("https-forward")

    # -- clean-up: only what this driver started or created --
    def sftp_units(self):
        """The helper's user units still loaded (its unit names, `terrace-lease-sftp-<16 hex>[-watch]`); None if the
        listing failed."""
        systemctl = SYSTEMCTL if os.path.exists(SYSTEMCTL) else "systemctl"
        code, out, _, _ = self.execute([systemctl, "--user", "list-units", "--all", "--plain", "--no-legend",
                                        "terrace-lease-sftp-*"], 30)
        if code != 0:
            return None
        return [line.split()[0] for line in out.decode("utf-8", "replace").splitlines() if line.strip()]

    def cleanup(self):
        """Every action runs whatever the others did; each failure is recorded; any unverified item means exit 3."""
        failures = self.cleanup_failures

        def attempt(label, action):
            try:
                if not action():
                    failures.append(label)
            except Exception as problem:  # noqa: BLE001 - one failure never skips the rest
                failures.append(f"{label} ({type(problem).__name__})")

        for name in list(self.processes):
            attempt(f"{name} not verified gone", lambda name=name: self.stop_background(name))
        if self.sftp_attempted:
            def sftp():
                result = self.command([self.tools.python, "-B", str(SFTP_TOOL), "stop", "--directory",
                                       str(self.run / SFTP)], 120, ok=None)
                left = self.sftp_units()
                self.note(f"SFTP stop exit {result.code}; helper units still loaded: {left}")
                # With no receipt (`start` interrupted before writing it) `stop` cannot name the units: the listing
                # decides. Anything left, or a listing that failed, is not verified.
                return left == [] and (result.code == 0 or not (self.run / SFTP / "sftp-listener.json").exists())
            attempt("the SFTP endpoint not verified stopped", sftp)
        for name in ("repository-password", "heartbeat.out", "wrong-known-hosts"):
            attempt(f"{name} not removed", lambda name=name: (remove_file(self.work / name),
                                                              not os.path.lexists(self.work / name))[1])

        def scratch():
            if self.work.exists():
                self.work.rmdir()
            return not os.path.lexists(self.work)
        attempt(f"{WORK}/ not removed", scratch)
        for item in failures:
            self.note(f"CLEAN-UP NOT VERIFIED: {item}")
        return not failures


def signed_by(output, target, names):
    """Which of NAMES ({file: key name}) signed TARGET, from `nix path-info --sigs`: `<path> <name>:<sig> ...`. Key
    names are compared exactly."""
    found = set()
    for line in output.splitlines():
        fields = line.split()
        if fields[:1] == [target]:
            found.update(token.split(":", 1)[0] for token in fields[1:] if ":" in token)
    return sorted(key for key, name in names.items() if name in found)


# ---- The steps ----------------------------------------------------------------------------------------------------

def step_2_1(d):
    o = {}
    d.expect((d.run / KNOWN_HOSTS).read_text() == pin_line(d.run, d.address, d.port),
             "the known-hosts file pins the secrets store's host key at this address and port")
    d.ssh("true", 60)
    o["operatorSsh"] = True
    # OpenSSH 10.6's PerSourcePenalties (on by default; the production sshd does not change them) refuse every new
    # connection from a source once its accrued penalty reaches `min` (15 s): a disconnect without authentication
    # costs 1 s, a failed authentication or a user not in AllowUsers 5 s. The four refusal probes back to back
    # accrue 16 s and the next connection is dropped (seen in the VM, dev run 3, 2026-11-27, and reproduced against
    # the same sshd). A penalty that has not reached `min` lapses after its own duration, so the probes run one at
    # a time, PROBE_PAUSE seconds apart (more than the 5 s a probe can cost): nothing accrues past 5 s, and the
    # operator login is proven before and again after them.
    pauses = []

    def probe(label, *, expected, **kwargs):
        result = d.ssh("true", 60, ok=(255,), **kwargs) if "argv" not in kwargs else \
            d.command(kwargs["argv"], 60, shown=kwargs["shown"], ok=(255,))
        seen = (result.err.strip().splitlines() or ["(nothing on standard error)"])[-1]
        d.expect(re.search(expected, result.err),
                 f"{label}: expected a refusal matching {expected!r}; seen: {seen}")
        time.sleep(PROBE_PAUSE)
        pauses.append(PROBE_PAUSE)

    wrong = d.work / "wrong-known-hosts"
    write_new(wrong, f"{known_hosts_name(d.address, d.port)} ssh-ed25519 {'A' * 68}\n")
    try:
        probe("a wrong pin", expected=r"Host key verification failed\.", known_hosts=wrong)
    finally:
        remove_file(wrong)
    probe("root with the operator key", expected=r"Permission denied \(publickey\)", user="root")
    probe("operator with the bootstrap key", expected=r"Permission denied \(publickey\)",
          identity=d.run / BOOTSTRAP_KEY)
    probe("root with the bootstrap key", expected=r"Permission denied \(publickey\)",
          identity=d.run / BOOTSTRAP_KEY, user="root")
    argv = [d.tools.ssh, *d.ssh_options(), "-o", "PubkeyAuthentication=no",
            "-o", "PreferredAuthentications=password,keyboard-interactive", f"operator@{d.address}", "true"]
    probe("password and keyboard-interactive (a refusal naming publickey only)",
          expected=r"Permission denied \(publickey\)\.", argv=argv,
          shown="ssh (password and keyboard-interactive only) operator@host true")
    d.ssh("true", 60)
    o["refused"] = ["wrong pin", "root", "bootstrap key (operator and root)", "password and keyboard-interactive"]
    o["sourcePenalties"] = {
        "observed": "the fourth probe back to back was dropped before authentication (PerSourcePenalties)",
        "pauseSeconds": pauses, "operatorAfterProbes": True,
    }
    o["bootId"] = d.boot_id()
    systems = d.systems()
    d.expect(systems["current"] == systems["booted"] == d.variants["v0"], "the host runs and booted V0")
    status = d.status()
    d.expect(status.splitlines()[:1] == ["not adopted: no writer starts at boot"], "not adopted")
    units = d.wanted_units()
    states = d.unit_states(["terrace-production.target", *units])
    d.expect(all(state["ActiveState"] == "inactive" for state in states.values()),
             "terrace-production.target and every unit it wants are inactive")
    o["writersInactive"] = units
    sops = d.unit_states(["sops-install-secrets.service"])["sops-install-secrets.service"]
    d.expect(sops["Result"] != "success" or sops["ActiveState"] != "active",
             f"sops-install-secrets has not succeeded ({sops})")
    o["sopsInstallSecrets"] = sops
    listen = d.ssh("ss -Htln", 30).out.split("\n")
    ports = sorted({line.split()[3].rsplit(":", 1)[1] for line in listen if line.strip()})
    d.expect(ports == ["22"], f"only TCP 22 listens ({ports})")
    o["listeningPorts"] = ports
    if ipaddress.IPv4Address(d.address).is_loopback:
        d.note("TCP 443 at the host's address: not applicable, the host is reached through a loopback forward of "
               "port 22 only (the provider firewall is what this checks at a lease)")
        o["tcp443"] = "not applicable (loopback forward)"
    else:
        try:
            socket.create_connection((d.address, 443), timeout=10).close()
            answered = True
        except OSError:
            answered = False
        d.expect(not answered, "TCP 443 at the host's address does not answer from the build machine")
        o["tcp443"] = "filtered"
    d.ssh("test -d /sys/firmware/efi", 30)
    # Recorded, not required: the installer writes only the private host key (docs/production-profile.md, cold start).
    o["hostKeyPubPresent"] = d.ssh("test -e /etc/ssh/ssh_host_ed25519_key.pub", 30, ok=(0, 1)).code == 0
    o["bootctl"] = d.root("bootctl status --no-pager 2>&1", 60, ok=None).out
    d.expect("systemd-boot" in o["bootctl"], "bootctl reports systemd-boot")
    addresses = json.loads(d.ssh("ip -4 -j addr show scope global", 30).out)
    dynamic = [(row["ifname"], info["local"], info["prefixlen"]) for row in addresses for info in row.get("addr_info", [])
               if info.get("dynamic")]
    d.expect(len(dynamic) >= 1, f"a global IPv4 address leased by DHCP ({dynamic})")
    o["dhcp"] = dynamic
    o["df"] = d.ssh("df -B1 --output=target,size,used,avail / /boot", 30).out
    closure = d.ssh("nix --extra-experimental-features nix-command path-info -S /run/current-system", 120).out.split()
    o["closureBytes"] = int(closure[-1])
    return o


def step_3_1(d):
    sftp = d.run / SFTP
    d.expect(not os.path.lexists(sftp / "sftp-listener.json") and (sftp / "sftp-host-key.pub").is_file(),
             "the SFTP directory is prepared and was never started")
    # Recorded BEFORE `start`: a start that is interrupted, times out or fails after launching a unit still gets a
    # stop and a check that no helper unit is left (clean-up).
    d.sftp_attempted = True
    result = d.command([d.tools.python, "-B", str(SFTP_TOOL), "start", "--directory", str(sftp), "--rclone",
                        d.tools.rclone, "--seconds", str(d.sftp_seconds)], 120)
    receipt = d.sftp_receipt()
    d.expect(receipt["hostKey"] == d.inputs["sftpHostKey"], "the endpoint serves the host key the build pinned")
    d.open_forward()
    return {"endpoint": json.loads(result.out), "backupPort": int(d.inputs["backupPort"])}


def step_3_2(d):
    d.expect((d.run / STAGE / "ssh_host_ed25519_key").is_file(), "the staged host key exists")
    result = d.command([d.tools.terrace_secrets, "push", HOST, "--address", d.address, "--port", str(d.port),
                        "--identity", str(d.run / OPERATOR_KEY)], 300, env=d.secrets_env())
    installed = re.search(r"^Installed ([0-9]+) encrypted secrets\.$", result.out, re.M)
    d.expect(installed and "Host state: not adopted" in result.out.splitlines(), "secrets installed; host not adopted")
    d.command([d.tools.terrace_secrets, "host", "unstage", HOST, "--stage", str(d.run / STAGE)], 60, env=d.secrets_env())
    d.expect(not os.path.lexists(d.run / STAGE), "the staging directory is gone")
    verify = d.root("terrace-secrets verify", 60).out
    d.expect("PASS" in verify, "terrace-secrets verify passes on the host")
    sops = d.unit_states(["sops-install-secrets.service"])["sops-install-secrets.service"]
    d.expect(sops["Result"] == "success", "sops-install-secrets succeeded")
    return {"secrets": int(installed[1]), "verify": verify.strip().splitlines()[-1:]}


def step_3_3(d):
    d.require_forward()
    result = d.root("terrace-production adopt --new", 600)
    d.expect("Adopted: the writers start now and at every boot." in result.out, "adopted")
    states = d.settled(cms_running=False)
    status = d.status()
    d.expect(re.match(r"adopted [0-9T:-]+Z\nactive\n", status), "status: adopted, target active")
    return {"units": states, "adoptSeconds": round(result.seconds, 1)}


def step_3_4(d):
    previous = json.loads(PREVIOUS_RELEASE.read_text())
    d.require_forward()
    migrate = d.root("systemctl start terrace-cms-migrate.service", 400)
    d.timings["migration-install"] = round(migrate.seconds, 1)
    d.expect(d.marker() == previous["revision"], "the marker holds the installed release's revision")
    d.expect(d.ledger() == previous["migrations"], "the ledger is the installed release's migration list")
    d.root("systemctl start terrace-cms.service terrace-web.service", 600)
    states = d.settled()
    d.root("terrace-backup init primary", 300)
    backup = d.root("systemctl start terrace-backup.service", 1200)
    d.timings["backup-first"] = round(backup.seconds, 1)
    d.expect(d.root("systemctl show terrace-backup.service -p Result --value", 30).out.strip() == "success",
             "the backup succeeded")
    snapshots = json.loads(d.root("terrace-backup restic primary snapshots --json", 300).out)
    d.expect(len(snapshots) == 1 and snapshots[0]["hostname"] == HOST, "one snapshot of host app in the repository")
    # The hourly freshness check fails until the first backup exists (docs/production-profile.md, cold start step 6);
    # run it once now, so a failure from before this backup does not stand.
    d.root("systemctl start terrace-backup-freshness.service", 120)
    offhost = restic_offhost(d, 1)
    return {"marker": previous["revision"], "migrationSeconds": round(migrate.seconds, 1),
            "backupSeconds": round(backup.seconds, 1), "snapshots": [snapshots[0]["short_id"]], "offHost": offhost,
            "units": states}


def restic_offhost(d, count):
    """`restic check --read-data` on the build machine, on the endpoint's own repository directory."""
    password = d.work / "repository-password"
    value = d.read_secret([d.tools.sops, "decrypt", "--extract", '["backup"]["primary"]["repository-password"]',
                           str(d.run / STORE / "hosts" / HOST / "secrets.sops.json")], 60, env=d.secrets_env(),
                          shown="sops decrypt --extract backup/primary/repository-password")
    d.redactor.add_bytes(value.encode(), "the backup repository password")
    try:
        write_new(password, value.strip() + "\n")
        repository = str(d.run / SFTP / "app")
        base = [d.tools.restic, "--repo", repository, "--password-file", str(password), "--no-cache"]
        check = d.command([*base, "check", "--read-data"], 1800)
        d.expect("no errors were found" in check.out, "restic check --read-data found no errors")
        snapshots = json.loads(d.command([*base, "snapshots", "--json"], 300).out)
    finally:
        remove_file(password)
    d.expect(len(snapshots) == count and all(item["hostname"] == HOST for item in snapshots),
             f"{count} snapshot(s) of host app, read on the build machine")
    return {"checkReadData": True, "snapshots": len(snapshots)}


def step_3_5(d):
    return d.https("3.5")


def step_4_1(d):
    facts = d.reboot("reboot-4.1")
    systems = d.systems()
    d.expect(systems["current"] == systems["booted"] == d.variants["v0"], "booted and current system V0")
    status = d.status()
    d.expect(re.match(r"adopted [0-9T:-]+Z\nactive\n", status), "adopted, target active")
    facts["units"] = d.settled()
    facts["forward"] = d.reopen_forward("the reboot ended every SSH connection")
    return facts


def update_guard(d):
    d.timers_clear()
    status = d.status()
    d.expect("deploy path: on, 2 trusted deploy key(s)" in status, "the deploy path is on with two trusted keys")


def start_record(d, label):
    """Step 5's record of where an update starts (docs/host-updates.md step 5)."""
    out = d.root("readlink -f /run/current-system; nix-env --profile /nix/var/nix/profiles/system --list-generations; "
                 "terrace-production status; systemctl --failed --no-legend; cat /var/lib/terrace-cms/migrated-revision; "
                 "terrace-backup age; systemctl list-timers --no-pager " + " ".join(TIMERS), 120, ok=None).out
    return {"label": label, "text": out}


def step_5_1(d):
    update_guard(d)
    deploy = d.deploy("v1", DEPLOY_KEY)
    d.expect(deploy["signedBy"] == [DEPLOY_KEY], "V1 carries the working deploy key's signature")
    before = start_record(d, "5.1")
    first = d.activate("activate", d.variants["v1"], "--dry-run")
    d.expect("Checks passed" in first.out, "dry run with the site up: exit 0 (the release does not change)")
    d.quiesce()
    d.activate("activate", d.variants["v1"], "--dry-run")
    result = d.activate("activate", d.variants["v1"])
    d.stop_timers_again()
    record = d.finished(d.records())
    d.expect(record["result"] == "success" and record["to"] == d.variants["v1"], "activate recorded success")
    d.expect(d.systems()["current"] == d.variants["v1"], "V1 runs")
    d.expect(d.ssh("cat /etc/terrace-lease-variant", 30).out.strip() == "v1", "V1's marker file")
    d.no_warning()
    d.activate("rollback", "--dry-run")
    back = d.activate("rollback")
    d.stop_timers_again()
    rollback = d.finished(d.records())
    d.expect(rollback["result"] == "success" and rollback["action"] == "rollback" and rollback["to"] == d.variants["v0"],
             "rollback recorded success to V0")
    d.expect(d.systems()["current"] == d.variants["v0"], "V0 runs again")
    d.expect(d.ssh("test ! -e /etc/terrace-lease-variant", 30, ok=(0,)).code == 0, "V1's marker file is gone")
    d.no_warning()
    d.restart_after_quiesce()
    d.settled()
    return {"deploy": deploy, "start": before, "activate": record, "activateSeconds": round(result.seconds, 1),
            "rollback": rollback, "rollbackSeconds": round(back.seconds, 1)}


def step_5_2(d):
    update_guard(d)
    pin = json.loads(NEXT_RELEASE.read_text())
    deploy = d.deploy("release", DEPLOY_KEY)
    d.expect(deploy["signedBy"] == [DEPLOY_KEY], "the release system carries the working deploy key's signature")
    before = start_record(d, "5.2")
    ledger_before = d.ledger()
    first = d.activate("activate", d.variants["release"], "--dry-run", ok=(3,))
    d.expect("Every other check passed" in first.out and "dry run NOT READY" in first.out,
             "dry run with the site up: exit 3, NOT READY")
    d.require_forward()
    # Step 6: quiesce, then the pre-update backup.
    d.quiesce()
    backup = d.root("systemctl start terrace-backup.service", 1200)
    d.timings["backup-pre-update"] = round(backup.seconds, 1)
    d.expect(d.root("systemctl show terrace-backup.service -p Result --value", 30).out.strip() == "success",
             "the pre-update backup succeeded")
    d.root("terrace-backup age", 60)
    snapshots = json.loads(d.root("terrace-backup restic primary snapshots --json", 300).out)
    d.expect(len(snapshots) == 2, "two snapshots now")
    second = d.activate("activate", d.variants["release"], "--dry-run")
    d.expect("Checks passed" in second.out, "dry run after the quiesce: exit 0")
    result = d.activate("activate", d.variants["release"])
    d.expect("THE SITE IS DOWN until the CMS migration" in result.out, "the client says the site is down until the migration")
    d.stop_timers_again()
    record = d.finished(d.records())
    d.expect(record["result"].startswith("switched; down until the CMS migration")
             and sorted(record["failedUnits"]) == ["terrace-cms.service", "terrace-web.service"]
             and record["unexpectedUnits"] == [] and record["notStartedUnits"] == [] and not record["waitTimedOut"],
             "recorded switched; down until the CMS migration, exactly the CMS and website")
    # Step 10: the migration, twice.
    migrate = d.root("systemctl start terrace-cms-migrate.service", 400)
    d.timings["migration-update"] = round(migrate.seconds, 1)
    d.expect(d.marker() == pin["revision"], "the marker holds the new release's revision")
    ledger = d.ledger()
    d.expect(ledger == pin["migrations"], "the ledger is the new pin's migration list")
    d.root("systemctl start terrace-cms-migrate.service", 400)
    d.expect(d.ledger() == ledger, "a second migration run applies nothing")
    # Step 11: start; step 12: checks; step 13: re-arm.
    d.root("systemctl reset-failed terrace-cms.service terrace-web.service", 60)
    d.root("systemctl start terrace-cms.service terrace-web.service", 600)
    d.root("systemctl start terrace-cms-sweep.timer", 60)
    d.expect(d.failed_units() == [""], "no failed unit")
    d.expect("PASS" in d.root("terrace-secrets verify", 60).out, "verify passes")
    d.root("terrace-tls check", 120)
    d.ssh("systemctl is-active terrace-cms-sweep.timer terrace-backup-freshness.timer terrace-identity-health.timer", 30)
    d.root("systemctl start terrace-backup.timer terrace-backup-check.timer", 60)
    d.settled()
    https = d.https("5.2")
    return {"deploy": deploy, "start": before, "ledgerBefore": ledger_before, "ledgerAfter": ledger, "activate": record,
            "migrationSeconds": round(migrate.seconds, 1), "backupSeconds": round(backup.seconds, 1), "https": https}


def step_5_3(d):
    update_guard(d)
    deploy = d.deploy("v2", SPARE_KEY)
    d.expect(deploy["signedBy"] == [SPARE_KEY], "V2 carries the spare key's signature and not the working key's")
    before = d.systems()
    generation = d.root("readlink /nix/var/nix/profiles/system", 30).out.strip()
    for args in (("--dry-run",), ()):
        refused = d.activate("activate", d.variants["v2"], *args, ok=(1,))
        d.expect("initrd differ(s) from the booted system; repeat with --boot" in refused.out,
                 f"activate {' '.join(args)} without --boot refused for the initrd")
    d.expect(d.systems() == before and d.root("readlink /nix/var/nix/profiles/system", 30).out.strip() == generation,
             "nothing changed")
    d.activate("activate", d.variants["v2"], "--boot", "--dry-run")
    result = d.activate("activate", d.variants["v2"], "--boot")
    d.expect("is the boot default." in result.out and "Reboot to run it" in result.out, "V2 is the boot default")
    record = d.finished(d.records())
    d.expect(record["result"] == "success" and record["mode"] == "boot", "recorded success, mode boot")
    status = d.status()
    d.expect("WARNING: the boot default" in status, "status warns that the boot default is not the running system")
    facts = d.reboot("reboot-5.3")
    systems = d.systems()
    d.expect(systems["booted"] == systems["current"] == d.variants["v2"], "/run/booted-system is V2")
    initrd = d.ssh(f"readlink -f /run/booted-system/initrd {shlex.quote(d.variants['v2'])}/initrd", 30).out.split()
    d.expect(len(initrd) == 2 and initrd[0] == initrd[1], "the booted initrd is V2's")
    d.expect(d.ssh("cat /etc/terrace-lease-variant", 30).out.strip() == "v2", "V2's marker file")
    d.no_warning()
    facts["units"] = d.settled()
    facts["esp"] = d.ssh("df -B1 --output=size,used,avail /boot", 30).out
    facts["entries"] = d.root("bootctl list --no-pager 2>/dev/null | grep -c '^ *title:' || true", 30).out.strip()
    facts["forward"] = d.reopen_forward("the reboot ended every SSH connection")
    return {"deploy": deploy, "record": record, "reboot": facts}


def step_5_4(d):
    update_guard(d)
    deploy = d.deploy("v3", DEPLOY_KEY)
    d.expect(deploy["signedBy"] == [DEPLOY_KEY], "V3 carries the working deploy key's signature")
    d.activate("activate", d.variants["v3"], "--dry-run")
    sshd_before = d.ssh("systemctl show sshd.service -p MainPID -p InvocationID --value", 30).out.split()
    beat = d.work / "heartbeat.out"
    d.start_background("second-session", d.ssh_argv("while :; do date +%s; sleep 1; done"), output=beat)
    time.sleep(5)
    beats_before = len(beat.read_text().split())
    d.expect(beats_before >= 2, "the second session is open and printing")
    forward_before = d.alive("reverse-forward")
    start = time.time()
    session = d.activate("activate", d.variants["v3"], ok=None, timeout=1200)
    time.sleep(5)
    beats_after = len(beat.read_text().split())
    second_alive = d.alive("second-session")
    time.sleep(3)
    still = len(beat.read_text().split()) > beats_after
    forward_after = d.alive("reverse-forward")
    reconnect = d.ssh("true", 60, ok=None).code == 0
    d.expect(reconnect, "the same key reconnects")
    record = d.finished(d.records())
    d.expect(record["result"] == "success" and record["to"] == d.variants["v3"], "the switch finished: recorded success")
    sshd_after = d.ssh("systemctl show sshd.service -p MainPID -p InvocationID --value", 30).out.split()
    loglevel = d.root("sshd -T 2>/dev/null | grep -i '^loglevel '", 30).out.strip()
    d.expect(loglevel.lower() == "loglevel verbose", "sshd runs V3's setting")
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(start - 5))
    journal = d.root(f"journalctl -u terrace-switch.service --since '{since} UTC' -o cat --no-pager | "
                     "grep -E 'sshd|restarting|stopping|starting|reloading' | head -n 40", 60, ok=None).out
    forward_works = d.forward_answers(int(d.inputs["backupPort"]))
    d.stop_background("second-session")
    observed = {
        "activatingSessionExit": session.code,
        "activatingSessionDropped": session.code == 255,
        "activatingSessionSawResult": "Running generation" in session.out,
        "secondSessionAlive": second_alive and still,
        "secondSessionBeatsDuringSwitch": beats_after - beats_before,
        "reverseForwardProcessAlive": forward_before and forward_after,
        "reverseForwardAnswers": forward_works,
        "sshdRestarted": sshd_before[:1] != sshd_after[:1],
        "sshdBefore": sshd_before, "sshdAfter": sshd_after,
        "sameKeyReconnects": reconnect,
        "switchJournal": journal,
    }
    d.note("5.4 observed: " + json.dumps({k: v for k, v in observed.items() if k != "switchJournal"}, sort_keys=True))
    observed["forwardAfter"] = d.reopen_forward("sshd was restarted by the switch")
    d.no_warning()
    return {"deploy": deploy, "record": record, "observed": observed, "activateSeconds": round(session.seconds, 1)}


def step_5_5(d):
    update_guard(d)
    v3 = d.variants["v3"]
    d.expect(d.systems()["current"] == v3, "V3 runs")
    deploy4 = d.deploy("v4", DEPLOY_KEY)
    d.activate("activate", d.variants["v4"], "--dry-run")
    result = d.activate("activate", d.variants["v4"], ok=(1,))
    d.expect("activate result: failed (exit 4)" in result.out, "the client reports failed (exit 4)")
    records = d.records()
    record = d.finished(records)
    d.expect(records[0].get("phase") == "started" and isinstance(records[0].get("failedBefore"), list),
             "the started record carries failedBefore")
    g4 = d.generation_of(d.variants["v4"])
    d.expect(record["result"] == "failed (exit 4)" and "terrace-lease-broken.service" in record["unexpectedUnits"]
             and record["revert"] is None and str(record["bootDefaultAfter"]) == str(g4),
             "V4 recorded failed (exit 4) naming terrace-lease-broken.service; no revert, it runs and is the boot default")
    d.expect(d.systems()["current"] == d.variants["v4"], "V4 runs (the switch had switched)")
    back = d.activate("rollback")
    rollback = d.finished(d.records())
    d.expect(rollback["result"] == "success" and rollback["to"] == v3, "rollback from V4 runs V3")
    d.expect(d.systems()["current"] == v3, "V3 runs again")
    deploy5 = d.deploy("v5", DEPLOY_KEY)
    d.activate("activate", d.variants["v5"], "--dry-run")
    failed = d.activate("activate", d.variants["v5"], ok=(1,))
    g3 = d.generation_of(v3)
    g5 = d.generation_of(d.variants["v5"])
    d.expect(f"so the boot default was set back to generation {g3}, the running system" in failed.out,
             "the client says the boot default was set back")
    record5 = d.finished(d.records())
    d.expect(record5["result"] == "failed (exit 1)" and record5["switchExit"] == 1
             and record5["revert"] == {"from": str(g5), "to": str(g3), "result": "success"}
             and str(record5["bootDefaultAfter"]) == str(g3) and str(record5["generationAfter"]) == str(g5),
             "V5 recorded failed (exit 1), revert success from V5 to V3, boot default V3")
    systems = d.systems()
    d.expect(systems["current"] == systems["bootDefault"] == v3, "V3 runs and is the boot default")
    d.no_warning()
    facts = d.reboot("reboot-5.5")
    systems = d.systems()
    d.expect(systems["booted"] == systems["current"] == v3, "the reboot boots V3, the reverted default")
    facts["units"] = d.settled()
    facts["forward"] = d.reopen_forward("the reboot ended every SSH connection")
    return {"v4": {"deploy": deploy4, "record": record, "client": result.out[-2000:], "rollback": rollback,
                   "rollbackSeconds": round(back.seconds, 1)},
            "v5": {"deploy": deploy5, "record": record5, "client": failed.out[-2000:]}, "reboot": facts}


def step_6(d):
    o = {}
    o["generations"] = d.root("nix-env --profile /nix/var/nix/profiles/system --list-generations", 60).out
    o["deployments"] = [json.loads(line) for line in d.root("cat /var/lib/terrace-production/deployments.log", 60)
                        .out.splitlines() if line.strip()]
    o["bootEntries"] = d.root("bootctl list --no-pager 2>&1 | grep -E '^ *(title|id|version|linux|initrd):' || true",
                              60).out
    o["df"] = d.ssh("df -B1 --output=target,size,used,avail / /boot", 30).out
    o["espUsage"] = d.root("du -sb /boot/EFI /boot/loader 2>/dev/null || true", 60).out
    errors = d.root("journalctl -p err -b --no-pager -o short-iso | tail -n 200", 60, ok=None).out
    o["journalErrorsThisBoot"] = errors
    o["failedUnits"] = d.failed_units()
    o["status"] = d.status()
    o["closureBytes"] = int(d.ssh("nix --extra-experimental-features nix-command path-info -S /run/current-system",
                                  120).out.split()[-1])
    o["snapshotsOffHost"] = restic_offhost(d, 2)
    return o


STEP_FUNCTIONS = {"2.1": step_2_1, "3.1": step_3_1, "3.2": step_3_2, "3.3": step_3_3, "3.4": step_3_4,
                  "3.5": step_3_5, "4.1": step_4_1, "5.1": step_5_1, "5.2": step_5_2, "5.3": step_5_3,
                  "5.4": step_5_4, "5.5": step_5_5, "6": step_6}


# ---- run ----------------------------------------------------------------------------------------------------------

def load_inputs(run):
    inputs = json.loads((run / INPUTS_JSON).read_text())
    if not isinstance(inputs.get("backupPort"), int) or not ED25519.fullmatch(inputs.get("sftpHostKey", "")) \
            or not re.fullmatch(r"age1[02-9ac-hj-np-z]{58}", inputs.get("escrowRecipient", "")):
        raise Refused("lease-inputs.json lacks backupPort, sftpHostKey or escrowRecipient")
    return inputs


def executable(tool, label):
    """A tool given by name (looked up on PATH) or by path; it must be an executable file."""
    path = tool if os.sep in tool else shutil.which(tool)
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise Refused(f"--{label}: {tool} is not an executable file")
    return path


def check_tools(tools, sftp_seconds):
    for label, tool in (("rclone", tools.rclone), ("restic", tools.restic), ("sops", tools.sops),
                        ("terrace-secrets", tools.terrace_secrets), ("curl", tools.curl), ("ssh", tools.ssh),
                        ("nix", tools.nix)):
        executable(tool, label)
    if not SFTP_SECONDS[0] <= sftp_seconds <= SFTP_SECONDS[1]:
        raise Refused(f"--sftp-seconds must be {SFTP_SECONDS[0]} to {SFTP_SECONDS[1]} (the helper's bounds)")
    for path in (DEPLOY_TOOL, SFTP_TOOL):
        if not path.is_file():
            raise Refused(f"{path.relative_to(ROOT)} is missing from this tree")
    for path in (PREVIOUS_RELEASE, NEXT_RELEASE):
        release = json.loads(path.read_text())
        if not re.fullmatch(r"[0-9a-f]{40}", release.get("revision", "")) or not release.get("migrations"):
            raise Refused(f"{path.relative_to(ROOT)} names no revision and migration list")


def ssh_public(keygen, path):
    """The public half of an SSH private key file, by `ssh-keygen -y`; None if it is not one."""
    try:
        result = subprocess.run([keygen, "-y", "-f", str(path)], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.decode("ascii", "replace").split()
    return " ".join(fields[:2]) if result.returncode == 0 and fields[:1] == ["ssh-ed25519"] else None


def store_path_exists(path):
    """A built system on this machine (one seam for the tests, which have no Nix store)."""
    return os.path.isdir(path)


def check_run(run, address, port, variants_path, tools=None, sftp_seconds=14400):
    run = private_directory(run)
    for name in (BOOTSTRAP_KEY, OPERATOR_KEY, DEPLOY_KEY, SPARE_KEY, ESCROW):
        private_file(run / name)
    for name in (KNOWN_HOSTS, RUN_CA, INPUTS_JSON, OPERATOR_KEY + ".pub"):
        if not (run / name).is_file():
            raise Refused(f"{name} missing from the run directory")
    for name in (STORE, SFTP):
        private_directory(run / name)
    if (run / KNOWN_HOSTS).read_text() != pin_line(run, address, port):
        raise Refused("the known-hosts file does not pin the secrets store's host key at this address and port")
    if nix_key_name(run / DEPLOY_KEY) == nix_key_name(run / SPARE_KEY):
        raise Refused("the two deploy keys must differ")
    if os.path.lexists(run / WORK):
        raise Refused(f"{WORK} exists in the run directory: an earlier run did not clean up")
    variants = read_variants(variants_path)
    if tools is not None:
        check_tools(tools, sftp_seconds)
        missing = [name for name, path in variants.items() if not store_path_exists(path)]
        if missing:
            raise Refused(f"variant store paths missing on this machine: {', '.join(missing)}")
        keygen = os.path.join(os.path.dirname(executable(tools.ssh, "ssh")), "ssh-keygen")
        keygen = keygen if os.access(keygen, os.X_OK) else executable("ssh-keygen", "ssh")
        bootstrap = ssh_public(keygen, run / BOOTSTRAP_KEY)
        operator = ssh_public(keygen, run / OPERATOR_KEY)
        if bootstrap is None or operator is None:
            raise Refused("the bootstrap key and the installed operator key must both be ed25519 SSH private keys")
        if operator != " ".join((run / (OPERATOR_KEY + ".pub")).read_text().split()[:2]):
            raise Refused("host-operator-ed25519.pub is not the operator key's public half")
        if bootstrap == operator:
            raise Refused("the bootstrap key must differ from the installed operator key (step 2.1 shows it refused)")
    return run, variants, load_inputs(run)


def step_receipt(driver, step, status, started, seconds, error, observations):
    receipt = {"step": step.id, "title": step.title, "status": status, "started": int(started),
               "seconds": seconds, "error": error}
    if status == "passed":
        try:
            driver.redactor.check(json.dumps(observations).encode(), f"step {step.id}'s observations")
            receipt["observations"] = observations
        except Leak as leak:
            receipt.update(status="failed", error=str(leak))
    try:
        driver.evidence.receipt(step.id, receipt)
        driver.evidence.ledger.append({"step": step.id, "seconds": seconds, "status": receipt["status"]})
        exit_word = {"passed": "exit 0", "interrupted": "interrupted"}.get(receipt["status"], "exit 1")
        driver.evidence.log(f"{step.id} {exit_word} {seconds}s {step.title}")
    except (OSError, TypeError, ValueError):
        receipt.update(status="failed", error=receipt.get("error") or "the step's receipt could not be written")
    return receipt["status"]


def run_steps(driver, steps):
    failed = None
    for step in steps:
        driver.evidence.open_step(step)
        start, started = time.monotonic(), time.time()
        status, observations, error = "passed", {}, None
        try:
            observations = STEP_FUNCTIONS[step.id](driver) or {}
        except Leak as leak:
            status, error = "failed", str(leak)
        except Failed as failure:
            status, error = "failed", str(failure)
        except Interrupted:
            # Signals are ignored from here (the handler did that); the step still gets its receipt.
            seconds = round(time.monotonic() - start, 1)
            driver.evidence.write(f"\n# step {step.id} interrupted after {seconds} s\n")
            driver.evidence.close_step()
            step_receipt(driver, step, "interrupted", started, seconds, "interrupted by a signal", {})
            driver.interrupted_step = step.id
            raise
        except Exception as problem:  # noqa: BLE001 - any other error fails the step; only its type is recorded
            status, error = "failed", f"{type(problem).__name__} in step {step.id}"
        seconds = round(time.monotonic() - start, 1)
        driver.evidence.write(f"\n# step {step.id} {status} after {seconds} s{': ' + error if error else ''}\n")
        driver.evidence.close_step()
        if step_receipt(driver, step, status, started, seconds, error, observations) != "passed":
            failed = step.id
            break  # G1: stop at the first failure, no retry.
    return failed


def load_store_secrets(driver):
    """Every value of the run's store into the redactor, read in memory (read_secret), never written."""
    driver.evidence.open_step(Step("redactor", "the store's values into the redactor", "none",
                                   f"every value of {MIN_SECRET_LENGTH} characters or more loaded", "transcript"))
    try:
        text = driver.read_secret([driver.tools.sops, "decrypt", "--input-type", "json", "--output-type", "json",
                                   str(driver.run / STORE / "hosts" / HOST / "secrets.sops.json")], 60,
                                  env=driver.secrets_env(), shown="sops decrypt (the run's store, into the redactor only)")
        return driver.redactor.add_store(json.loads(text))
    finally:
        driver.evidence.close_step()


def cmd_run(args):
    try:
        address = checked_address(args.address, args.port)
        tools = Tools(rclone=args.rclone, restic=args.restic, sops=args.sops, terrace_secrets=args.terrace_secrets,
                      curl=args.curl, ssh=args.ssh_binary, nix=args.nix)
        run, variants, inputs = check_run(args.run, address, args.port, args.variants, tools, args.sftp_seconds)
        steps = select_steps(args.steps, args.start, args.end)
        if args.dry_run:
            print(f"Inputs accepted; would run against {known_hosts_name(address, args.port)}:")
            print(listing(steps), end="")
            return EXIT_PASS
        evidence = Evidence(args.evidence)
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, Refused) else f"{type(error).__name__} while reading inputs"
        print(f"lease_host_steps: refused: {message}", file=sys.stderr)
        return EXIT_INPUTS
    driver = Driver(run=run, address=address, port=args.port, evidence=evidence, tools=tools, variants=variants,
                    inputs=inputs, sftp_seconds=args.sftp_seconds)
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def stop(_signum, _frame):
        # The first signal interrupts; from then on every signal is ignored until the evidence is complete.
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        raise Interrupted()

    previous = {sig: signal.signal(sig, stop) for sig in signals}
    failed = redactor = None
    interrupted = False
    clean = False
    try:
        (run / WORK).mkdir(mode=0o700)
        for name in (BOOTSTRAP_KEY, OPERATOR_KEY, DEPLOY_KEY, SPARE_KEY, ESCROW, SFTP + "/sftp-host-key"):
            driver.redactor.add_file(run / name)
        evidence.log(f"run start: {len(steps)} step(s) {' '.join(step.id for step in steps)}; "
                     f"womb {inputs.get('wombRevision', 'unrecorded')}")
        try:
            redactor = load_store_secrets(driver)
            evidence.log(f"redactor exit 0: {redactor['loaded']} store values loaded")
        except (Failed, OSError, ValueError, KeyError, TypeError) as problem:
            failed = "redactor"
            evidence.log(f"redactor exit 1: {problem if isinstance(problem, Failed) else type(problem).__name__}")
        if failed is None:
            failed = run_steps(driver, steps)
    except Interrupted:
        interrupted = True
        try:
            evidence.close_step()
            evidence.log(f"interrupted in step {getattr(driver, 'interrupted_step', None) or 'between steps'}")
        except Exception:  # noqa: BLE001 - the evidence write must not skip the clean-up
            pass
    except Exception as problem:  # noqa: BLE001 - an error outside the steps still reaches clean-up and evidence
        failed = failed or f"driver ({type(problem).__name__})"
    finally:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        try:
            evidence.open_step(Step("cleanup", "host-independent clean-up", "always runs (G1)",
                                    "forwards and second session stopped; SFTP endpoint stopped; scratch removed",
                                    "transcript"))
            clean = driver.cleanup()
            evidence.close_step()
            evidence.log(f"cleanup {'exit 0' if clean else 'exit 1'}")
        except Exception:  # noqa: BLE001 - an unverifiable clean-up is exit 3
            clean = False
    code = EXIT_CLEANUP if not clean else EXIT_INTERRUPTED if interrupted else EXIT_STEP_FAILED if failed else EXIT_PASS
    try:
        evidence.finish({"result": {0: "pass", 1: "step failed", 3: "clean-up not verified", 4: "interrupted"}[code],
                         "exit": code, "failedStep": failed, "interruptedStep": getattr(driver, "interrupted_step", None),
                         "cleanupNotVerified": driver.cleanup_failures, "redactor": redactor,
                         "steps": [step.id for step in steps], "timings": driver.timings,
                         "address": known_hosts_name(address, args.port), "wombRevision": inputs.get("wombRevision")})
    except Exception:  # noqa: BLE001 - say so; the exit code still comes from the state
        print("lease_host_steps: WARNING: the summary or SHA256SUMS could not be written", file=sys.stderr)
    for sig, handler in previous.items():
        signal.signal(sig, handler)
    print(f"lease_host_steps: {('PASS' if code == 0 else 'FAILED')} (exit {code}); evidence in {evidence.directory}")
    return code


# ---- prepare (run sheet step 0.6) and pin -------------------------------------------------------------------------

def cmd_prepare(args):
    """The run material, every file created new inside RUN; private halves never printed."""
    try:
        run = private_directory(args.run)
        names = (OPERATOR_KEY, OPERATOR_KEY + ".pub", DEPLOY_KEY, DEPLOY_KEY[:-4] + ".pub", SPARE_KEY,
                 SPARE_KEY[:-4] + ".pub", ESCROW, RUN_CA, STORE, STAGE, SFTP, SFTP_CLIENT, INPUTS_JSON, INPUTS_NIX)
        if any(os.path.lexists(run / name) for name in names):
            raise Refused("run material already exists in this directory")
        for path in (args.web_archive, args.cms_archive, args.previous_web_archive, args.previous_cms_archive):
            if not Path(path).is_file():
                raise Refused("a release archive is missing")
        if not Path(args.tfc_source).is_dir() or not re.fullmatch(r"[0-9a-f]{40}", args.womb_revision):
            raise Refused("--tfc-source must be a directory and --womb-revision a full commit")
        if not 1024 <= args.backup_port <= 65535:
            raise Refused("--backup-port must be unprivileged")
    except (Refused, OSError) as error:
        print(f"lease_host_steps: refused: {error}", file=sys.stderr)
        return EXIT_INPUTS
    tag = args.key_tag or secrets.token_hex(4)
    env = {key: value for key, value in os.environ.items() if key not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    secrets_env = dict(env, TERRACE_SECRETS_STORE=str(run / STORE), SOPS_AGE_KEY_FILE=str(run / ESCROW))

    def call(argv, *, stdin=None, environment=None, capture=False, report=False):
        """REPORT: the command prints names and fixed lines only (`terrace-secrets check`), so its output may be shown."""
        result = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE if capture or report else subprocess.DEVNULL,
                                stderr=subprocess.PIPE if report else subprocess.DEVNULL, env=environment or env,
                                timeout=300, check=False)
        if report:
            print((result.stdout + result.stderr).decode("utf-8", "replace").strip())
        if result.returncode:
            raise Failed(f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} failed (exit {result.returncode})")
        return result.stdout

    scratch = None
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def stop(_signum, _frame):
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        raise Interrupted()

    # A signal must not skip the wipe of the run CA key and the leaf files (the default action of TERM and HUP ends
    # the process without running `finally`).
    previous = {sig: signal.signal(sig, stop) for sig in signals}
    code = EXIT_PASS
    try:
        call([args.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"terrace-lease-operator-{tag}", "-f",
              str(run / OPERATOR_KEY)])
        for name, file in (("deploy", DEPLOY_KEY), ("spare", SPARE_KEY)):
            secret = call([args.nix, "--extra-experimental-features", "nix-command", "key", "generate-secret",
                           "--key-name", f"terrace-lease-{tag}-{name}-1"], capture=True)
            write_new(run / file, secret)
            public = call([args.nix, "--extra-experimental-features", "nix-command", "key", "convert-secret-to-public"],
                          stdin=secret, capture=True)
            write_new(run / (file[:-4] + ".pub"), public, 0o644)
        call([args.terrace_secrets, "escrow", "create", str(run / ESCROW)], environment=secrets_env)
        # The recipient the host is built with (escrowRecipient) is the one every later `check` and `push` compares.
        secrets_env["TERRACE_ESCROW_RECIPIENT"] = (run / STORE / "escrow.recipient").read_text().strip()
        call([args.terrace_secrets, "host", "create", HOST, "--role", "app", "--stage", str(run / STAGE)],
             environment=secrets_env)
        call([args.terrace_secrets, "generate", HOST], environment=secrets_env)
        # The run CA and the leaf for the three names; the CA key and the leaf files never outlive this command.
        scratch = Path(tempfile.mkdtemp(prefix="tls-", dir=run))
        ca_key, leaf_key, leaf, request, ext = (scratch / n for n in ("ca-key.pem", "leaf-key.pem", "leaf.pem",
                                                                       "leaf.csr", "leaf.ext"))
        call([args.openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(ca_key)])
        call([args.openssl, "req", "-x509", "-new", "-key", str(ca_key), "-days", str(LEAF_DAYS), "-subj",
              f"/CN=Terrace lease run CA {tag}", "-addext", "basicConstraints=critical,CA:TRUE",
              "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", str(run / RUN_CA)])
        os.chmod(run / RUN_CA, 0o644)
        call([args.openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(leaf_key)])
        call([args.openssl, "req", "-new", "-key", str(leaf_key), "-subj", f"/CN={NAMES[0]}", "-out", str(request)])
        write_new(ext, "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                       "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(f"DNS:{n}" for n in NAMES) + "\n")
        call([args.openssl, "x509", "-req", "-in", str(request), "-CA", str(run / RUN_CA), "-CAkey", str(ca_key),
              "-set_serial", str(int.from_bytes(os.urandom(8), "big")), "-days", str(LEAF_DAYS), "-extfile", str(ext), "-out", str(leaf)])
        # The host's `terrace-tls check` (and its daily expiry timer) fails a certificate with fewer than
        # TLS_CHECK_DAYS left: the leaf must clear it for the whole run.
        call([args.openssl, "x509", "-noout", "-checkend", str((TLS_CHECK_DAYS + 7) * 86400), "-in", str(leaf)])
        call([args.terrace_secrets, "set", HOST, "tls/certificate"], stdin=leaf.read_bytes(), environment=secrets_env)
        call([args.terrace_secrets, "set", HOST, "tls/key"], stdin=leaf_key.read_bytes(), environment=secrets_env)
        call([args.terrace_secrets, "check", HOST], environment=secrets_env, report=True)
        client = call([args.terrace_secrets, "public", HOST, "backup/primary/sftp-key"], environment=secrets_env,
                      capture=True)
        write_new(run / SFTP_CLIENT, client, 0o644)
        (run / SFTP).mkdir(mode=0o700)
        call([sys.executable, "-B", str(SFTP_TOOL), "prepare", "--directory", str(run / SFTP), "--ssh-keygen",
              args.ssh_keygen, "--client-public-key", str(run / SFTP_CLIENT)])
        sftp_host = (run / SFTP / "sftp-host-key.pub").read_text().strip()
        operator_public = " ".join((run / (OPERATOR_KEY + ".pub")).read_text().split()[:2])
        deploy_public = [(run / (f[:-4] + ".pub")).read_text().strip() for f in (DEPLOY_KEY, SPARE_KEY)]
        escrow = (run / STORE / "escrow.recipient").read_text().strip()
        inputs = {"schema": 1, "disk": args.disk, "operatorKeys": [f"{operator_public} terrace-lease-operator-{tag}"],
                  "escrowRecipient": escrow, "deployKeys": deploy_public, "runCa": str(run / RUN_CA),
                  "backupPort": args.backup_port, "backupHostKey": sftp_host, "sftpHostKey": sftp_host,
                  "webArchive": str(Path(args.web_archive).resolve()), "cmsArchive": str(Path(args.cms_archive).resolve()),
                  "previousWebArchive": str(Path(args.previous_web_archive).resolve()),
                  "previousCmsArchive": str(Path(args.previous_cms_archive).resolve()),
                  "tfcSource": str(Path(args.tfc_source).resolve()), "wombRevision": args.womb_revision, "keyTag": tag}
        write_new(run / INPUTS_JSON, json.dumps(inputs, indent=1, sort_keys=True) + "\n", 0o644)
        nix = ["{"]
        for key in ("disk", "escrowRecipient", "backupHostKey", "webArchive", "cmsArchive", "previousWebArchive",
                    "previousCmsArchive", "tfcSource", "wombRevision"):
            nix.append(f"  {key} = {json.dumps(inputs[key])};")
        nix.append(f"  operatorKeys = [{json.dumps(inputs['operatorKeys'][0])}];")
        nix.append(f"  deployKeys = [{' '.join(json.dumps(k) for k in deploy_public)}];")
        nix.append(f"  runCa = {run / RUN_CA};")
        nix.append(f"  backupPort = {args.backup_port};")
        nix.append("}")
        write_new(run / INPUTS_NIX, "\n".join(nix) + "\n", 0o644)
    except (Failed, OSError, subprocess.SubprocessError) as error:
        # Paths name files, never their contents: the file name is safe to print.
        what = str(error) if isinstance(error, Failed) else type(error).__name__ + (
            f" ({error.filename})" if isinstance(error, OSError) and error.filename else "")
        print(f"lease_host_steps: prepare failed: {what}; remove the run directory and start again", file=sys.stderr)
        code = EXIT_STEP_FAILED
    except Interrupted:
        print("lease_host_steps: prepare interrupted; remove the run directory and start again", file=sys.stderr)
        code = EXIT_INTERRUPTED
    finally:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        left = wipe_scratch(scratch)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if left:
        # Names only, never contents.
        print(f"lease_host_steps: prepare could NOT remove: {', '.join(left)}; remove them by hand", file=sys.stderr)
        return EXIT_CLEANUP
    if code != EXIT_PASS:
        return code
    print(f"Run material written in {run}: {', '.join(names)}. Build with: (import infra/nixos/lease-host.nix "
          f"(import {run / INPUTS_NIX})).install and .variants")
    return EXIT_PASS


def wipe_scratch(scratch):
    """Overwrite and remove every file of prepare's TLS scratch directory, then the directory; one failure never stops
    the rest. Returns what is still there (names, relative to the run directory)."""
    left = []
    if scratch is None or not os.path.lexists(scratch):
        return left
    try:
        names = os.listdir(scratch)
    except OSError:
        return [scratch.name]
    for name in names:
        try:
            remove_file(scratch / name)
        except OSError:
            pass
        if os.path.lexists(scratch / name):
            left.append(f"{scratch.name}/{name}")
    try:
        scratch.rmdir()
    except OSError:
        pass
    if os.path.lexists(scratch) and not left:
        left.append(scratch.name)
    return left


def cmd_pin(args):
    try:
        run = private_directory(args.run)
        address = checked_address(args.address, args.port)
        if os.path.lexists(run / KNOWN_HOSTS):
            raise Refused("the known-hosts file already exists")
        write_new(run / KNOWN_HOSTS, pin_line(run, address, args.port), 0o600)
    except (Refused, OSError) as error:
        print(f"lease_host_steps: refused: {error}", file=sys.stderr)
        return EXIT_INPUTS
    print(f"Pinned {known_hosts_name(address, args.port)} to the secrets store's host key in {run / KNOWN_HOSTS}")
    return EXIT_PASS


def parser():
    top = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = top.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the steps with their guards, expected observations and evidence")
    prepare = sub.add_parser("prepare", help="run sheet step 0.6: the run material")
    prepare.add_argument("run", type=Path)
    for name in ("terrace-secrets", "openssl", "ssh-keygen", "web-archive", "cms-archive", "previous-web-archive",
                 "previous-cms-archive", "tfc-source", "womb-revision"):
        prepare.add_argument("--" + name, required=True)
    prepare.add_argument("--nix", default="nix")
    prepare.add_argument("--disk", default="/dev/sda")
    prepare.add_argument("--backup-port", type=int, default=22433)
    prepare.add_argument("--key-tag", help=argparse.SUPPRESS)
    pin = sub.add_parser("pin", help="write the pinned known-hosts line for the installed host")
    pin.add_argument("run", type=Path)
    pin.add_argument("--address", required=True)
    pin.add_argument("--port", type=int, default=22)
    run = sub.add_parser("run", help="run steps 2.1 to 6 against the installed host")
    run.add_argument("run", type=Path)
    run.add_argument("--address", required=True, help="the host's IPv4 address as the build machine reaches it")
    run.add_argument("--port", type=int, default=22)
    run.add_argument("--evidence", type=Path, required=True, help="a new directory for transcripts and receipts")
    run.add_argument("--variants", type=Path, required=True, help="lease-host.nix's `variants` output")
    for name in ("rclone", "restic", "sops", "terrace-secrets"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--curl", default="curl")
    run.add_argument("--ssh", dest="ssh_binary", default="ssh")
    run.add_argument("--nix", default="nix")
    run.add_argument("--sftp-seconds", type=int, default=14400)
    selection = run.add_mutually_exclusive_group()
    selection.add_argument("--steps", help="comma-separated step ids (development)")
    selection.add_argument("--from", dest="start", choices=STEP_IDS)
    run.add_argument("--to", dest="end", choices=STEP_IDS)
    run.add_argument("--dry-run", action="store_true", help="validate the inputs and list the selected steps only")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    if args.command == "list":
        print(listing(), end="")
        return EXIT_PASS
    if args.command == "prepare":
        return cmd_prepare(args)
    if args.command == "pin":
        return cmd_pin(args)
    if args.steps and args.end:
        print("lease_host_steps: refused: --to goes with --from, not --steps", file=sys.stderr)
        return EXIT_INPUTS
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
