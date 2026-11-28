"""VM rehearsal of the production lease host: install it the lease's way, then run the host steps driver against it.

One UEFI/KVM guest shaped like the leased CPX42 (OVMF, one 320 GiB sparse virtio-SCSI disk beside a CD-ROM, a virtio
NIC on QEMU's user network, which hands out the address by DHCP) boots the pinned Debian 13 stock image, as the
provider's server does. The fixture then takes the lease's own path, with the same committed tools:

1. `lease_host_steps.py prepare` makes the run material (step 0.6) in the run directory, and the lease host builder
   (`lease-host.nix`) builds V0's install outputs and the seven variant systems from it (step 0.7);
2. the stock guest proves its host key through the keyed banner, `stock_kexec.dispatch` kexecs the RAM installer,
   `stock_kexec.verify_ram` and `inspect_ram_target` record the disk receipt (step 1.5);
3. `lease_install_plan.py` writes the plan and the installed host key from the secrets store's staged key, and
   `rescue_install.install` installs V0 onto the disk with the approved digest (step 1.6);
4. a QMP reset stands in for the provider's reboot into the disk (step 1.7): the firmware boots systemd-boot through
   the removable `\\EFI\\BOOT\\BOOTX64.EFI`, and the installed system takes its address by DHCP;
5. `lease_host_steps.py pin` and `run` carry out steps 2.1 to 6 over pinned SSH on the forwarded loopback port,
   including the real reboots through the boot loader (4.1, 5.3, 5.5).

Only the provider binding, the bootstrap review and the store copy's address are substituted (as in
test_stock_kexec_native.py). The guest is stopped when the fixture ends, also after a failure, and verified gone.
docs/cloud-rehearsal.md, "VM rehearsal of the host steps", says how to rerun it and what it cannot show.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import lease_host_steps as steps
import lease_install_plan as plan
import rescue_install as install
import stock_kexec
import test_stock_kexec_native as native

PHASE, ROLE = "original", "app"
PREFIX = f"{PHASE}-{ROLE}"
CONTEXT = (native.FIXTURE_RUN, PHASE, ROLE)
ALIAS = "terrace-lease-host-fixture"
HERE = Path(__file__).resolve().parent


class Stage:
    def __init__(self, facts):
        self.facts = facts

    def __call__(self, name):
        self.facts["stage"] = name
        self.facts.setdefault("stageStarts", {})[name] = int(time.time())


def logged(argv, log, seconds, cwd=None, env=None):
    """A long local command whose output goes to an evidence log file (no secrets pass through these)."""
    with open(log, "ab") as stream:
        stream.write(f"$ {' '.join(argv)}\n".encode())
        stream.flush()
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=stream, timeout=seconds, check=False)
        stream.write(f"# exit {result.returncode}\n".encode())
    install.require(result.returncode == 0, f"{Path(argv[0]).name} failed; see {Path(log).name}")
    return result.stdout.decode()


def prepare_and_build(args, run, evidence, facts):
    revision = subprocess.run(["git", "-C", str(args.checkout), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    facts["wombRevision"] = revision
    logged([sys.executable, "-B", str(HERE / "lease_host_steps.py"), "prepare", str(run),
            "--terrace-secrets", args.terrace_secrets, "--openssl", args.openssl, "--ssh-keygen", args.ssh_keygen,
            "--web-archive", args.web_archive, "--cms-archive", args.cms_archive,
            "--previous-web-archive", args.previous_web_archive, "--previous-cms-archive", args.previous_cms_archive,
            "--tfc-source", args.tfc_source, "--womb-revision", revision], evidence / "prepare.log", 600)
    outputs = {}
    for name in ("install", "variants"):
        start = time.monotonic()
        expression = f"(import {args.checkout}/infra/nixos/lease-host.nix (import {run / steps.INPUTS_NIX})).{name}"
        out = logged(["nix", "build", "--impure", "--option", "builders", "", "--print-out-paths",
                      "--out-link", str(run / f"gcroot-{name}"), "--expr", expression],
                     evidence / f"build-{name}.log", 4 * 3600, cwd=args.checkout)
        outputs[name] = out.strip()
        facts[f"build{name.title()}Seconds"] = round(time.monotonic() - start)
    install_outputs = json.loads(Path(outputs["install"]).read_text())
    variants = json.loads(Path(outputs["variants"]).read_text())
    install.require(install_outputs["system"] == variants["v0"], "V0 of the variants is the installed system")
    sizes = logged(["nix", "path-info", "-S", install_outputs["system"], install_outputs["disko"],
                    install_outputs["installer"], *variants.values()], evidence / "sizes.log", 300)
    (evidence / "outputs.json").write_text(json.dumps({"install": install_outputs, "variants": variants,
                                                       "sizes": sizes.splitlines()}, indent=1) + "\n")
    return run / "gcroot-install", run / "gcroot-variants", install_outputs


def boot_installed(args, run, process, qmp, facts):
    """The provider's reboot into the disk: a reset; then the installed system must answer the pinned operator key."""
    with native.qmp_commands(qmp) as ask:
        install.require(ask("system_reset") == {}, "guest reset failed")
    start = time.monotonic()
    install.require(subprocess.run([sys.executable, "-B", str(HERE / "lease_host_steps.py"), "pin", str(run),
                                    "--address", "127.0.0.1", "--port", str(args.port)],
                                   stdout=subprocess.DEVNULL, check=False).returncode == 0, "pin refused")
    ssh = ["ssh", "-F", "/dev/null", "-i", str(run / steps.OPERATOR_KEY), "-o", "IdentitiesOnly=yes",
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={run / steps.KNOWN_HOSTS}",
           "-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519", "-o", "ConnectTimeout=10",
           "-p", str(args.port), "operator@127.0.0.1"]
    native.await_fact(process, 600, lambda: install.remote(ssh, 20, "true"))
    facts["installedBootSeconds"] = round(time.monotonic() - start)
    # Read-only: the firmware used the removable loader path (no variable names this disk).
    raw = install.remote(ssh, 10, 'if test -e "$1"; then base64 -w0 "$1"; fi', native.LOADER_IMAGE, capture=True)
    loader = base64.b64decode(raw)[4:].decode("utf-16-le").rstrip("\0") if raw else None
    facts["installedLoaderImage"] = loader
    install.require(loader is not None and loader.lower() == "\\efi\\boot\\bootx64.efi",
                    "installed system did not boot from the fallback loader")


def run_driver(args, run, variants, evidence, facts):
    argv = [sys.executable, "-B", str(HERE / "lease_host_steps.py"), "run", str(run), "--address", "127.0.0.1",
            "--port", str(args.port), "--evidence", str(evidence / "driver"), "--variants", str(variants),
            "--rclone", args.rclone, "--restic", args.restic, "--sops", args.sops,
            "--terrace-secrets", args.terrace_secrets]
    if args.steps:
        argv += ["--steps", args.steps]
    start = time.monotonic()
    with open(evidence / "driver.out", "wb") as out:
        driver = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        try:
            code = driver.wait()
        except BaseException:
            # Interrupted: the driver runs its own clean-up on SIGTERM.
            driver.send_signal(signal.SIGTERM)
            driver.wait(timeout=600)
            raise
    facts["driverExit"] = code
    facts["driverSeconds"] = round(time.monotonic() - start)
    return code


def exercise(args, run, evidence, facts):
    stage = Stage(facts)
    stage("prepare-build")
    install_out, variants_out, outputs = prepare_and_build(args, run, evidence, facts)
    stage("guest")
    nonce = secrets.token_hex(32)
    native.private_text(run / f"{PREFIX}-bootstrap-nonce", nonce + "\n")
    bootstrap_public = native.key(run / steps.BOOTSTRAP_KEY)
    seed = run / "seed.img"
    native.render_seed(args, run, seed, nonce, bootstrap_public, prefix=PREFIX, context=CONTEXT)
    overlay = run / "guest.qcow2"
    template = Path(args.ovmf_vars).read_bytes()
    install.require(0 < len(template) <= 8 * 1024 * 1024, "unexpected OVMF variable store")
    with os.fdopen(os.open(run / "efi-vars.fd", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(template)
    install.checked_process([args.qemu_img, "create", "-f", "qcow2", "-F", "qcow2", "-b", str(args.base.resolve()),
                             str(overlay), f"{native.FIXTURE_DISK_GB}G"], 10)
    options = [*install.ssh_options(run, PREFIX, "bootstrap"), "-o", "HostKeyAlias=" + ALIAS, "-p", str(args.port)]
    ssh = ["ssh", *options, "root@127.0.0.1"]
    now = int(time.time())
    stock_binding = {"run": native.FIXTURE_RUN, "phase": PHASE, "role": ROLE, "serverId": 1, "ipv4": "127.0.0.1",
                     "expires": now + 86400, "observedAt": now}
    binding = dict(stock_binding, ipv4=native.FIXTURE_IP)
    serial = bytearray()
    public = None
    monitor = tempfile.mkdtemp(prefix="terrace-lease-qmp-", dir="/tmp")
    qmp = Path(monitor) / "qmp.sock"
    try:
        with native.guest(args, overlay, seed, serial, facts, qmp, run / "efi-vars.fd",
                          memory_mib=args.memory_mib, cpus=args.cpus) as process, \
                patch.object(install, "bootstrap_authority", side_effect=lambda *_a: (stock_binding, public, options, ssh)), \
                patch.object(install, "remaining", side_effect=lambda _d, _b, limit: limit):
            stage("proof")
            public = native.await_proof(process, 300, nonce, ["-p", str(args.port), "-o", "HostKeyAlias=" + ALIAS],
                                        context=CONTEXT)
            native.private_text(run / f"{PREFIX}-bootstrap-known-hosts", f"{ALIAS} ssh-ed25519 {public}\n")
            stage("stock")
            native.await_fact(process, 300, lambda: stock_kexec.stock_boot(ssh, public, run))
            stage("dispatch")
            try:
                stock_kexec.dispatch(run, PHASE, ROLE, args.archive, args.archive_sha256)
            except install.Rejected:
                # The live SSH session can end during the kernel switch.
                if not (run / f"{PREFIX}-kexec-dispatched.json").is_file():
                    raise
            stage("ram")
            stock_kexec.verify_ram(run, PHASE, ROLE)
            ready, _ = install.read_json(run / f"{PREFIX}-ram-ready.json")
            facts["ramWaitSeconds"] = ready["waitSeconds"]
            bound = (binding, public, options, ssh)
            with patch.object(install, "authority", return_value=bound), \
                    patch.object(install, "reviewed_probe_mode", return_value=False), \
                    patch.object(install, "reviewed_bootstrap", return_value=(native.FIXTURE_RUN, "0" * 64)):
                stage("disk-receipt")
                _, _, disk, _ = stock_kexec.inspect_ram_target(run, PHASE, ROLE, native.FIXTURE_DISK_GB)
                install.require(disk["device"] == native.FIXTURE_DISK, "the receipt names another disk")
                stage("plan")
                approved = plan.record_plan(run, PHASE, ROLE, install_out, run / steps.STAGE,
                                            run / steps.STORE / "hosts" / ROLE / "ssh_host_ed25519_key.pub")
                facts["planSha256"] = approved
                stage("install")
                copy = install.copy_store

                def loopback_copy(copy_binding, copy_options, seconds, paths, *, target=False):
                    return copy({**copy_binding, "ipv4": "127.0.0.1"}, copy_options, seconds, paths, target=target)

                start = time.monotonic()
                with patch.object(install, "copy_store", side_effect=loopback_copy):
                    try:
                        install.install(run, PHASE, ROLE, approved, None)
                    except install.InstallFailed as failure:
                        facts["installFailure"] = {"label": failure.label, "line": failure.line, "status": failure.status}
                        raise
                facts["installSeconds"] = round(time.monotonic() - start)
            installed, _ = install.read_json(run / f"{PREFIX}-installed.json")
            install.require(installed["system"] == outputs["system"], "installed receipt names another system")
            # The installed key copy is not needed any more: the host has it, the store keeps the public half.
            steps.remove_file(run / f"{PREFIX}-installed-ed25519")
            stage("installed-boot")
            boot_installed(args, run, process, qmp, facts)
            stage("host-steps")
            code = run_driver(args, run, variants_out, evidence, facts)
            install.require(code == 0, f"host steps driver exit {code}")
            stage("complete")
    finally:
        forbidden = {b"PRIVATE KEY-----"}
        for name in (steps.BOOTSTRAP_KEY, steps.OPERATOR_KEY, steps.DEPLOY_KEY, steps.SPARE_KEY, steps.ESCROW):
            path = run / name
            if path.exists():
                lines = path.read_bytes().splitlines()
                forbidden.update(line.strip() for line in (lines[1:-1] if len(lines) > 2 else lines) if len(line) > 16)
        facts["serialPrivateKeyFound"] = any(marker in serial for marker in forbidden)
        facts["serialBytesCaptured"] = len(serial)
        lower = serial.lower()
        facts["serialMarkers"] = {name: any(m.lower() in lower for m in markers)
                                  for name, markers in native.SERIAL_MARKERS.items()}
        os.rmdir(monitor) if os.path.isdir(monitor) and not os.listdir(monitor) else None
        facts["qmpScratchGone"] = not os.path.exists(monitor)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="a new 0700 directory on scratch storage for the run's disks and keys")
    parser.add_argument("--evidence", type=Path, required=True, help="a new directory for the evidence")
    parser.add_argument("--checkout", type=Path, required=True, help="a clean checkout of this revision")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--memory-mib", type=int, default=16384, help="the CPX42's 16 GB")
    parser.add_argument("--cpus", type=int, default=8, help="the CPX42's 8 vCPUs")
    parser.add_argument("--steps", help="development: pass a step selection to the driver")
    for tool in ("qemu", "qemu-img", "mkfs-vfat", "mcopy", "ovmf-code", "ovmf-vars", "openssl", "ssh-keygen",
                 "terrace-secrets", "rclone", "restic", "sops", "web-archive", "cms-archive", "previous-web-archive",
                 "previous-cms-archive", "tfc-source"):
        parser.add_argument("--" + tool, required=True)
    args = parser.parse_args()
    facts = {"stage": "inputs", "memoryMiB": args.memory_mib, "cpus": args.cpus}
    code = 1
    try:
        args.evidence.mkdir(mode=0o700)
        output = install.private_directory(args.output)
        install.require(1024 <= args.port <= 65535, "unsafe fixture transport")
        install.require(native.base_digest(args.base) == native.BASE_SHA512, "Debian base digest differs")
        run = output / "run"
        run.mkdir(mode=0o700)
        try:
            exercise(args, run, args.evidence, facts)
            code = 0
        finally:
            facts["baseUnchanged"] = native.base_digest(args.base) == native.BASE_SHA512
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, InterruptedError) as error:
        facts["error"] = str(error) if isinstance(error, install.Rejected) else type(error).__name__
    finally:
        facts["guestOff"] = facts.get("qemuOff")
        with open(args.evidence / "fixture.json", "w") as stream:
            json.dump(facts, stream, indent=1, sort_keys=True)
    print("Lease host VM rehearsal " + ("passed" if code == 0 else f"failed at stage {facts['stage']}")
          + "; the guest is off." * bool(facts.get("qemuOff")))
    return code


def stop(_signum, _frame):
    raise InterruptedError("lease host VM rehearsal interrupted")


if __name__ == "__main__":
    os.umask(0o077)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    sys.exit(main())
