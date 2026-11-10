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

The stock route starts with a reviewed disposable Debian 13 cohort. Cloud-init
deletes the image host keys and generates a fresh Ed25519 key on the host, so no
host private key leaves it. User data carries only a one-use proof nonce and the
public run, phase and role. A first-boot script publishes
`terrace-hostkey-proof v1 <HMAC-SHA256(nonce, "terrace-hostkey-v1|run|phase|role|<public key>")>`
as the SSH pre-authentication banner, then removes its nonce copy. Use a separate
installed host key. The installer archive and Nix store receive no private
material. The runtime installer copies the self-generated key across kexec in RAM.

The pin step opens one SSH session that offers no client authentication, records
the presented Ed25519 key only in a throwaway file and reads the banner from the
same encrypted transport. The key is pinned only when the MAC verifies with the
reviewed nonce. A missing proof is retried for a bounded time; a wrong one aborts.
A network attacker cannot produce a valid MAC for a substituted key, and nothing
is sent before verification. The nonce is still in the provider, the guest
metadata service and, through the hcloud bridge, the private plan. It is a
low-value secret: one host key, one run, used once.

Build the public archive on Falcon under the shared build lock. Verify and
record the exact archive SHA256 before dispatch:

```sh
direnv exec . nix eval --json --impure --file infra/nixos/kexec-installer.nix proof
direnv exec . nix build --impure --file infra/nixos/kexec-installer.nix archive \
  --no-link --print-out-paths --max-jobs 1 --option builders ""
```

Use the resulting `nixos-kexec-installer-x86_64-linux.tar.gz`. For each host,
the pin step writes `<phase>-<role>-bootstrap-known-hosts`, its fingerprint and
`<phase>-<role>-bootstrap-proof.json` exactly once, bound to the authenticated
provider binding. Later steps require that proof receipt for the same server
and address before SSH. These commands assume a fresh provider binding at each
step:

```sh
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" --pin-proof
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" \
  --dispatch "$archive" "$archive_sha256"
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" --verify-ram
```

Dispatch checks the operator key and the proven Ed25519 host key on stock
Debian. The published host key line may carry ssh-keygen's `root@host` comment;
only its type and key are compared. Each check is its own command, because
under `set -e` a failure inside an `&&` list neither stops the script nor
fires its trap. A failed check prints only its line number in the fixed
script and its status. Dispatch rejects and reports just those two integers:
`Stock host check failed at line N (status S)`. Any other output is
rejected without detail.

The effective sshd settings, evaluated from `/` for the session's own
addresses and port, must also leave no other login route. `AuthorizedKeysFile` is the Debian
default; `AuthorizedKeysCommand`, `AuthorizedPrincipalsCommand`,
`AuthorizedPrincipalsFile`, `TrustedUserCAKeys` and `HostKeyAgent` are
`none`; there is no `HostCertificate`.

Three allowances cover provider differences without widening trust:

- Cloud-init may exit 2 only when its JSON status is `done`, its extended status
  is `degraded done`, it lists no errors and no module failed. The module check
  matches cloud-init's "Running module … failed" wording, so a differently worded
  failure is bounded only by the independent checks. Provider datasources and vendor
  data can raise recoverable warnings, and every trust-relevant effect is
  checked independently afterwards.
- A provider sshd drop-in may name other `HostKey` paths only if each one is a
  plain absolute path that does not exist, since sshd serves only existing keys.
  The Ed25519 key pair must still be the only `ssh_host_*` files.
- Provider metadata can repeat the operator key, as Hetzner offers the
  project SSH key. `authorized_keys` may hold one to four lines, each that
  same key with no options.

The check requires UEFI boot (`/sys/firmware/efi`) because the install layout
is UEFI only, and refuses a kernel under lockdown, which would block the
unsigned kexec. It stays strict on `cpio` and `gzip`: the upstream runner
packs the carried state with them. The disk probe, which also runs in the
kexec'd installer, does not require the installer to see EFI; the stock check
before kexec is the firmware gate. Format
and install verify that the vfat `/mnt/boot` is the EFI system partition
(type `c12a7328-…`) of the same disk, and that `nixos-install` left
`EFI/BOOT/BOOTX64.EFI` and `loader/loader.conf`. The QEMU fixture offers the
operator key as metadata `public-keys` like Hetzner does. It also proves that a
mismatched operator key is reported at the `authorized_keys` comparison line.

Dispatch verifies
the archive in a private, size-bounded tmpfs mounted on a new leaf under
`/run`. A 4 GiB Debian fixture had only 410,771,456 free bytes on its
existing `/run`, too little for this installer. Dispatch checks available
RAM against the compressed and unpacked sizes, verifies the private mount
and removes the compressed copy before kexec. An upload failure removes the
new mount and leaf. The connection can close during the switch. If the dispatch
receipt exists, check the new boot instead of dispatching again.
The upstream runner alone runs with umask 022 inside that 0700 tmpfs. It copies
`/etc/machine-id` with plain `cp`; a root-only copy stops networkd from
starting its DHCP clients, which fails the restored link before any address.
Verification requires the same SSH pin, a different kernel
boot ID, the pinned installer marker and the RAM-backed root and store.
NixOS remounts the store read-only over itself, so every mount stacked at
`/nix/store` must be an overlay.

The runner kexecs 6 s after dispatch returns, and the kernel then unpacks the
xz initrd that carries the whole RAM system. Verification therefore retries
for up to 150 s, which the native fixture also uses, and rechecks the provider binding
before it writes `<phase>-<role>-ram-ready.json`. It reduces each attempt to one
fixed state:
- `not-ram`: something with the pin answered but failed the RAM checks, as stock Debian does before the switch.
- `stock-boot`.
- `no-answer`: no connection within ssh's 10 s limit, or within the last attempt's shorter budget.
- `refused` or `closed`.
- `no-banner` or `stalled`: the connection was made, but sshd stayed silent.
- `ssh-other`, including output over 4 KiB.
- `ready`.

ssh's whole `Host key verification failed.` or `Permission denied` line
stops the wait at once as `host-key` or `denied`. Text a peer echoes inside
ssh's other messages before key exchange is never final. ssh's own messages
are never kept. On failure, verification
prints only the state transitions and whole seconds, at most 16 of them,
keeping the first 15 and the last, for example
`RAM installer not ready after 150 s; observed not-ram@0 no-answer@3. Private
inputs suppressed.` A timeline that ends in `no-answer` after earlier answers
means the host did not come back on the network within the wait. A late
`ready` means a slow boot.
On success the receipt adds the timeline and, as best effort, the RAM system's own
boot timing: EFI visibility, uptime, and when the initrd, the switched root and
`sshd` started.
On 2026-11-09 the OVMF fixture of `20010fb` observed
`not-ram@0 no-banner@6 ready@18`. QEMU's user network accepts TCP while the
guest is down. The RAM system saw EFI and started its initrd at 4.9 s, its
switched root at 8.9 s and `sshd` at 12.7 s after kernel start.
The probe-only cohort ends here. A full cohort next checks the RAM installer
as an install target, then records its whole-disk probe:

```sh
direnv exec . python3 -B infra/nixos/stock_kexec.py "$lease" "$phase" "$role" --inspect-target "$disk_gb"
```

`$disk_gb` is the server type's disk class: 320 for the CPX42 app and 80
for the CPX22 relay. The check requires a KVM or QEMU guest whose route to the
internet leaves from the bound address, and exactly one whole `sd`, `vd`,
`xvd` or `nvme` disk. A CD-ROM beside it is allowed. The disk must be writable,
non-removable and unmounted, and between `N`×10⁹ bytes and `N` GiB with 4 MiB of slack.
The whole-disk probe then checks every stock partition as before and writes
`<phase>-<role>-disk.json`. A failure prints one fixed line, for example
`Install target check failed: single-disk. Private inputs suppressed.` The
labels are `ram-authority`, `target-facts`, `virtualization`, `route-source`,
`single-disk`, `disk-name`, `disk-state`, `disk-size`, `whole-disk-probe`,
`probe-boot-id`, `probe-alias`, `probe-whole-disk`, `probe-identity`,
`probe-partition-state`, `probe-holders` and `disk-receipt`. A failing line
in either fixed script adds only its line number and status, as in
`whole-disk-probe line 9 status 1`. After that the reviewed install above runs. Omit the Rescue bootstrap archive
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
The fixture creates a temporary operator key and proof nonce and calls the same
cloud-config renderer as the creator. The guest generates its own host key; the
fixture pins it only through the keyed banner proof, and requires a wrong nonce
and a wrong phase context to be rejected against the live guest. It attaches a private read-only NoCloud CIDATA
CD-ROM, as described in the [cloud-init NoCloud documentation](https://github.com/canonical/cloud-init/blob/25.1.4/doc/rtd/reference/datasources/nocloud.rst),
formatted with 2048-byte sectors to suit the CD-ROM's block size.
The disk is shaped like the CPX42 app disk: one 320 GiB virtio-SCSI disk,
`/dev/sda`, beside the CD-ROM `sr0`. The stock image keeps its GPT layout,
an ext4 root that cloud-init grows to fill the disk, BIOS boot and the EFI
system partition. In the RAM installer, the fixture requires the install
target check to accept that disk. It then requires these four rejections:
- a wrong bound address gives `route-source`;
- an 80 GB class gives `disk-size`;
- a second disk hot-plugged over QMP gives `single-disk`;
- a mounted stock ESP gives `probe-partition-state`.

Finally it writes the whole-disk receipt. Hetzner's own Debian 13 image layout
and its disk bus are not measured by any receipt yet, so the provider run reports
them through the same labels.

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
    --qemu "$qemu" --qemu-img "$qemu_img" --mkfs-vfat "$mkfs_vfat" --mcopy "$mcopy" \
    --ovmf-code "$ovmf/FV/OVMF_CODE.fd" --ovmf-vars "$ovmf/FV/OVMF_VARS.fd"
```

Here `$ovmf` is the `fd` output of the pinned nixpkgs `OVMF` package. The
single UEFI/KVM guest boots its read-only `OVMF_CODE.fd` with
a private writable copy of the variable store, uses a disposable overlay, and its only forwarded
port binds loopback. The NoCloud seed assigns a fixed IPv4 address to the
fixture NIC by MAC and checks that address on both boots. This isolates the
offline trust handoff from QEMU DHCP, following the [upstream static-address test](https://github.com/nix-community/nixos-images/blob/50939ddf1a0339f289a9dbd810bdfd4e4fba6df4/nix/kexec-installer/test.nix).
Provider networking still needs its own probe.
The test substitutes the lease transport for that local port, uses the
production SSH settings and remote checks, and requires
OpenSSH to reject a wrong key on both boots. It requires Nix store copy to
reject the wrong key and leave the public sentinel absent, then copies the
sentinel with the reviewed key and verifies its digest in RAM. A stock-boot
failure records fixed boot markers and, if reached, the SSH stage and failing
stock check line and status. When cloud-init's completion check fails, it
records only the numeric status, allowlisted state and error counts.
If the upload fails, the fixture records its failing line and status plus
the two tmpfs checks, free byte counts and available memory from the same
pinned guest.
For the handoff it records the kexec command status and fixed boot markers.
It also records, without requiring it, whether the RAM installer still sees
EFI after kexec; installation writes no firmware variables.
A RAM readiness failure records SSH authentication, whether the boot ID
changed and the failing RAM check when the reviewed host key authenticates.
The fixture classifies selected systemd units from the bounded serial data.
If SSH gives no banner, it can read 4,000 bytes of local VGA text memory
through a private Unix QMP socket and retain only fixed boot facts.
For network diagnostics it attaches a capture filter after the archive
upload. The filter writes to a private FIFO, and the fixture reads a
bounded memory buffer and reports only fixed DHCP, ARP and SSH header
counts. No packet capture file is retained.
The test discards raw diagnostics and the bounded serial
sample and removes the temporary overlay and keys after stopping the guest.
No disk formatter or application starts in this proof.

On 2026-11-07 the pinned base reached a Debian login prompt in a separate
25-second offline BIOS boot with standard VGA and no NIC or cloud-init seed.
The trust fixture includes that display hardware while keeping display
rendering disabled. The boot-only check did not exercise SSH or kexec.

On 2026-11-07 the offline proof of source `584574b` passed in 82 seconds with
the archive SHA256 `62ea7c1419a927d3400831de0340613a971471e1fbdb114b8587de6287257831`.
The RAM guest completed one DHCP exchange and kept the fixture address. The
same pin authenticated a new boot, a wrong pin and wrong-key Nix copy were
rejected, and the reviewed copy verified. A separate offline-only diagnostic
measured the earlier failure as networkd `failed` with DHCP client permission
errors. Provider networking still needs its own probe.
