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

Dispatch checks the sole operator key on stock Debian and stages the archive in
root-only tmpfs. The connection can close during the switch. If the dispatch
receipt exists, check the new boot instead of dispatching again.
Verification requires the same SSH pin, a different kernel
boot ID, the pinned installer marker and the RAM-backed root and store.
The probe-only cohort ends here. A full cohort can subsequently run the
whole-disk probe and reviewed install above. Omit the Rescue bootstrap archive
on this route because the RAM installer supplies Nix. Disko still requires the
separate reviewed disk and install-plan digests. The legacy Rescue route uses
its original console pin and separate Nix bootstrap archive.

The [pinned standalone kexec runner](https://github.com/nix-community/nixos-images/blob/aa38dbbdf0e955baef7e03dfc4265ae3fdac4808/nix/kexec-installer/kexec-run.sh)
and its [initrd handoff](https://github.com/nix-community/nixos-images/blob/aa38dbbdf0e955baef7e03dfc4265ae3fdac4808/nix/restore-remote-access.nix)
carry the network state and SSH files. The controller calls the standalone
runner over the already pinned connection and retains strict verification
after the boot.
