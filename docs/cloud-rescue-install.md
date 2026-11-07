# Install one leased Rescue host

The executor handles a single synthetic app or relay host from the cloud
rehearsal. The provider controller creates and rechecks its lease first.
Collect the Rescue SSH host fingerprint through the authenticated provider
console for that exact server. SSH discovery can supply the public key
bytes, but the executor requires them to match the independent console
fingerprint. Use separate pins for Rescue and the installed OS. See
[Hetzner's Rescue guide](https://docs.hetzner.com/cloud/servers/getting-started/rescue-system/)
and [provider console guide](https://docs.hetzner.com/cloud/servers/getting-started/vnc-console/).

Keep all state in the existing owner-only lease directory. The controller
supplies `lease.json`, the phase's `*-created.json`, and a fresh per-role
binding such as `original-app-binding-<time_ns>.json` with `run`, `phase`, `role`,
`serverId`, `ipv4`, `expires` and `observedAt`. The install executor accepts
only a server created in that phase and a provider observation at most
120 seconds old. It selects the newest exact per-role receipt and preserves
earlier observations. It takes no provider credential.

Place the run operator key in `operator-ed25519`, the single exact Rescue
known_hosts entry in `original-app-rescue-known-hosts`, and the independently
observed `SHA256:…` in `original-app-rescue-fingerprint`. Generate a separate
per-host Ed25519 host key in `original-app-installed-ed25519` and put its
public pin for the same IP in `original-app-installed-known-hosts`. Use mode
0600 and no symbolic links for these files.

### Headless provider console

Before the lease starts, build the console helper on Falcon under the
shared build lock. The helper packages the pinned noVNC client, Chromium,
Playwright, and English Tesseract data from the platform lock.

```sh
nix build --no-link --print-out-paths --impure --file infra/nixos/console-fingerprint.nix
```

After the controller confirms Rescue is ready, it can request the console
for the exact lease-owned server. Supply the resulting mode-0600
`original-app-console-<time_ns>.json` by absolute path before its short
connection deadline. Run the helper under a bounded user unit on Falcon.

```sh
systemd-run --user --wait --collect --pipe \
  --property=RuntimeMaxSec=125s --property=TimeoutStopSec=5s \
  --property=KillMode=control-group \
  "$CONSOLE_PACKAGE/bin/terrace-console-fingerprint" \
  /protected/lease original app "$PROTECTED_CONSOLE_RECEIPT"
```

The helper compares the independently authenticated Rescue console key to
the SSH discovery candidate. It requires a new, exact console marker from
the comparison before writing both Rescue pins. Console frames, credentials
and URLs stay out of logs. On failure, the private per-attempt JSON records
the fixed last stage and bounded diagnostic facts. Check that the user unit and its processes have
stopped before another attempt. The helper does not retry or replace any
existing pin files. An interrupted publication can leave one file present.
Review that state before proceeding.

For a reviewed `probeOnly` original relay, pass `--keyboard-probe-only`
after the console receipt path in the command above, and select `relay`.
The helper compares immediate and paced entry of a fixed public sample on
the active getty line and verifies the line is empty again after each trial.
It records only the echo classification and erase check. This mode sends
no Enter or login credentials and creates no SSH pin. Its success output
identifies keyboard calibration only.

Use `--physical-keyboard-probe-only` in place of `--keyboard-probe-only`
for one paced sample with physical key codes. The helper requires the
server's QEMU extended key-event acknowledgement before sending any key.
It presses and releases Shift explicitly for uppercase letters and checks
the same active prompt is empty after erasing. The separate private result
records readiness, acknowledgement, echo classification and the erase
check. This mode also ends before login or SSH pinning.

```sh
direnv exec . python3 -B infra/nixos/test_console_fingerprint.py -v
```

### Inspect and install

First collect the read-only disk receipt from the builder:

```sh
direnv exec . python3 -B infra/nixos/rescue_install.py /protected/lease original app \
  --inspect-disk /dev/disk/by-id/VERIFIED-WHOLE-DISK
```

Review the private `original-app-disk.json` including the server binding,
Rescue boot ID, whole disk, size, and kernel identity. Use that same device
in the pinned local Nix role config. Build the three outputs from
[the cloud role runbook](cloud-rehearsal.md). Record them as `disko`,
`installer` and `system` in `original-app-install.json`, with
`diskSha256` equal to the disk receipt's SHA256. Review the resulting install
plan and keep its exact SHA256. If Rescue reboots, get a new disk receipt
before approving a new plan.

Fetch the [official Nix 2.35.2 Linux x86_64 tarball](https://releases.nixos.org/nix/nix-2.35.2/nix-2.35.2-x86_64-linux.tar.xz)
on the builder and verify SHA256
`0c3960a9792331a22081c3c7a5d8465db9b17c50b3acdf18587fa4c6f2cb1158`
from the [pinned upstream installer](https://releases.nixos.org/nix/nix-2.35.2/install).
The executor verifies the archive again before using it to initialize the
temporary Rescue Nix store. It copies the large system closure directly
to the mounted target store, keeping it out of Rescue RAM.
The measured synthetic gateway closure is about 10 GB. The target copy
allows up to 30 minutes for the gateway or seven minutes for the relay,
subject to the remaining lease. The remote store process has an independent
stop deadline and a ten-second kill grace.

With the lease still armed and the provider binding refreshed, the
reviewed destructive step is:

```sh
direnv exec . python3 -B infra/nixos/rescue_install.py /protected/lease original app \
  --install-approved-sha "$REVIEWED_INSTALL_PLAN_SHA256" \
  --bootstrap-archive /protected/reviewed/nix-2.35.2-x86_64-linux.tar.xz
```

The executor rechecks the disk and provider binding before Disko. The
provider controller can publish a new observation while the Rescue tools
are copied. The immutable binding must match. The executor leaves the
provider lock free for the reaper and checks the lease deadline and
`closed.json` before each mutating phase. It creates an exclusive dispatch
record before formatting, verifies the
mounted ext4 partition is on the reviewed disk, installs the exact
closure, and stages the independently pinned installed host key. Reboot
is a separate controller step. A failure after dispatch requires operator
review and scoped lease cleanup. This procedure is offline-tested against
synthetic inputs. Actual Rescue boot and installation still require the
provider rehearsal.

```sh
direnv exec . python3 -B infra/nixos/test_rescue_install.py -v
```

## Pinned Debian-to-RAM bootstrap

The stock route starts with a reviewed disposable Debian 13 cohort whose
cloud-init data installs the per-host Ed25519 key. The reviewed input carries
the matching public key. Keep the bootstrap key and cloud-init payload private
and use a separate installed host key. The provider and stock guest receive the
bootstrap private key, including through the guest-local metadata endpoint.
The installer archive and Nix store receive no private material. The runtime
installer copies the bootstrap key across kexec in RAM.

Build the public archive on Falcon under the shared build lock. Verify and
record the exact archive SHA256 before dispatch:

```sh
direnv exec . nix eval --json --impure --file infra/nixos/kexec-installer.nix proof
direnv exec . nix build --impure --file infra/nixos/kexec-installer.nix archive \
  --no-link --print-out-paths --max-jobs 1 --option builders ""
```

Use the resulting `nixos-kexec-installer-x86_64-linux.tar.gz`. For each host,
stage the reviewed public key in the owner-private lease directory as
`<phase>-<role>-bootstrap-known-hosts` and its fingerprint as
`<phase>-<role>-bootstrap-fingerprint`. Use the authenticated provider binding
for the known-hosts IP. The controller checks the local pin against the public
key bound to the dispatched review before SSH. These commands assume a fresh
provider binding at both steps:

```sh
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" \
  --dispatch "$archive" "$archive_sha256"
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" --verify-ram
```

Dispatch checks the sole operator key and exact Ed25519 host key on stock
Debian. Cloud-init writes the reviewed public key without a terminal newline.
The check accepts that one-line file with or without the newline. It verifies
the archive in a private directory under `/dev/shm` and unpacks it under `/run` so the
compressed and unpacked copies do not compete for the same tmpfs limit. It
checks both mount capacities and available RAM against the measured archive
sizes. The connection can close during the switch. If the dispatch
receipt exists, check the new boot instead of dispatching again.
Verification requires the same SSH pin, a different kernel
boot ID, the pinned installer marker and the RAM-backed root and store.
The probe-only cohort ends here. A full cohort can subsequently run the
whole-disk probe and reviewed install above. Omit the Rescue bootstrap archive
on this route because the RAM installer supplies Nix. Disko still requires the
separate reviewed disk and install-plan digests. The legacy Rescue route uses
its original console pin and separate Nix bootstrap archive.

The [pinned standalone kexec runner](https://github.com/nix-community/nixos-images/blob/50939ddf1a0339f289a9dbd810bdfd4e4fba6df4/nix/kexec-installer/kexec-run.sh)
and its [initrd handoff](https://github.com/nix-community/nixos-images/blob/50939ddf1a0339f289a9dbd810bdfd4e4fba6df4/nix/restore-remote-access.nix)
carry the network state and SSH files. The installer uses upstream's locked
NixOS 26.05, which still exposes its required initrd option. The app and Disko
keep their existing pins. The controller retains strict SSH verification
throughout.
The archive keeps upstream's runner and static kexec binary. The runner's
three network-inventory calls use the authenticated stock guest's
[Debian iproute2 binary](https://packages.debian.org/trixie/amd64/iproute2/filelist).

### Offline trust proof

Use the [Debian 13 genericcloud image and published SHA512 list](https://cloud.debian.org/images/cloud/trixie/20261103-2651/)
for build 20261103-2651. The test verifies the exact amd64 QCOW2 digest
before and after use. Supply the evaluated archive and its reviewed SHA256.
The fixture creates temporary keys and calls the same cloud-config renderer
as the creator. It attaches a private read-only NoCloud CIDATA drive, as
described in the [cloud-init NoCloud documentation](https://github.com/canonical/cloud-init/blob/25.1.4/doc/rtd/reference/datasources/nocloud.rst).

On Falcon, hold the shared build lock and use one supervised service scope.
Set the public store sentinel to a small file added with `nix store add-file`,
then point the tool variables at the reviewed Linux binaries. Use a fresh
owner-only output directory outside the checkout and an unused loopback port.

```sh
systemd-run --user --wait --collect --pipe --unit=terrace-stock-kexec-proof \
  --property=RuntimeMaxSec=600 --property=TimeoutStopSec=15 \
  --property=KillMode=control-group \
  direnv exec . python3 -B infra/nixos/test_stock_kexec_native.py "$output" \
    --base "$debian_image" --archive "$archive" --archive-sha256 "$archive_sha256" \
    --store-probe "$sentinel_store_path" --port "$loopback_port" \
    --qemu "$qemu" --qemu-img "$qemu_img" --mkfs-vfat "$mkfs_vfat" --mcopy "$mcopy"
```

The single BIOS/KVM guest uses a disposable overlay, and its only forwarded
port binds loopback. The test substitutes the lease transport for that local
port, uses the production SSH settings and remote checks, and requires
OpenSSH to reject a wrong key on both boots. It requires Nix store copy to
reject the wrong key and leave the public sentinel absent, then copies the
sentinel with the reviewed key and verifies its digest in RAM. A stock-boot
failure records fixed boot markers and, if reached, the SSH stage and failing
stock check line and status. When cloud-init's completion check fails, it
records only the numeric status, allowlisted state and error counts.
The test discards raw diagnostics and the bounded serial
sample and removes the temporary overlay and keys after stopping the guest.
No disk formatter or application starts in this proof.

On 2026-11-07 the pinned base reached a Debian login prompt in a separate
25-second offline BIOS boot with standard VGA and no NIC or cloud-init seed.
The trust fixture includes that display hardware while keeping display
rendering disabled. The boot-only check did not exercise SSH or kexec.
