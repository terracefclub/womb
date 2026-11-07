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
