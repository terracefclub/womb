# Private two-host cloud rehearsal

The disposable gateway and independent Headscale relay reuse the accepted
native modules and the same synthetic identity and application fixture. The
gateway uses the [reviewed Linux release pair](website-host.md) and the
hash-checked TFC identity producer. Both profiles boot with their writers
inactive. The relay has no K3s. This composition has no public application
hostname or ACME account.

## Evaluate and prepare the installation

On the Linux builder, run the configuration check before building anything:

```sh
direnv exec . nix eval --raw --impure --file infra/nixos/cloud-rehearsal-check.nix
```

Prepare an owner-only local Nix module per leased host, with its verified
whole disk, Rescue hardware configuration without filesystem declarations,
network settings and the run-specific operator's **public** SSH key. Do not
put the provider token, TLS key, browser fixture, or repository credentials
into this module. The controller must bind the leased provider ID, Rescue
host key and actual disk identity before any install command.

The installer profile evaluates the pinned Disko and NixOS outputs. From
the repository root on the Linux builder, supply the verified host inputs:

```sh
args=(--impure --file infra/nixos/rehearsal-config.nix
      --argstr role relay --arg localModule /absolute/private/relay-host.nix
      --argstr privateIPv4 "$REHEARSAL_RELAY_IPV4")
direnv exec . nix eval "${args[@]}" config.boot.loader.grub.devices
direnv exec . nix eval "${args[@]}" config.fileSystems
```

For the gateway, use `--argstr role gateway` and supply
`--arg webArchive /absolute/reviewed/web.tar.gz`,
`--arg cmsArchive /absolute/reviewed/cms.tar.gz` and
`--argstr tfcSource /absolute/reviewed/tfc-source` in place of `privateIPv4`.
The three matching outputs are `config.system.build.diskoScript`,
`config.system.build.toplevel` and `config.system.build.nixos-install`.
Build each under the shared Linux builder lock with
`direnv exec . nix build "${args[@]}" --no-link --print-out-paths <output>`.
Record the three exact output paths in the private run manifest. The relay
has a separate closure and receives no app archives.

On the matched, leased Rescue host with a working Nix store, copy the three
reviewed closures using host-key-pinned SSH. The following is the direct
install sequence **only after rechecking the leased ID, verified whole disk,
BIOS mode, empty `/mnt` and fresh disposable role**:

```sh
"$DISKO_SCRIPT"
findmnt --mountpoint /mnt --noheadings --output SOURCE,FSTYPE
"$INSTALLER/bin/nixos-install" --root /mnt --system "$NIXOS_TOPLEVEL" --no-root-passwd --no-channel-copy
```

The Disko script destroys and formats the single configured disk. Verify
that the mounted ext4 root belongs to the same verified whole disk before
running the installer. Obtain and pin the installed host key independently
before operator SSH. Rescue availability, actual firmware, disk, reboot and
service health are measured provider steps.

## Keep the data and checks private

- Provider ingress and the NixOS global firewall allow operator SSH only.
  The gateway permits identity and the synthetic mail sink on its local
  Podman bridge. Use SSH loopback forwards for gateway and relay HTTPS.
  The relay's local dummy interface carries its synthetic RFC1918 /32 and
  STUN binds there. This requires no provider network and proves no external
  UDP reachability or forced DERP.
- Generate the run CA and the gateway and relay leaves in private operator
  storage. Install each leaf and key in root-owned mode-0700
  `/var/lib/terrace-rehearsal-tls` with the key mode-0400. Install the **public**
  CA as root-owned mode-0644 `/var/lib/terrace-rehearsal-trust/ca.pem` in a
  root-owned mode-0755 directory. The startup guard checks the exact synthetic
  hostnames, server certificate purpose, non-CA leaf and key match before
  nginx starts. Keep the CA key on the operator only.
- Use a bounded isolated operator on the Linux builder. Use a pinned SSH
  local HTTPS forward from an isolated loopback listener on port 443 to
  `127.0.0.1:443` on the gateway. Map the six gateway `*.terrace.invalid`
  names to that listener in the operator environment. Map
  `relay.terrace.invalid` to a separate loopback listener for the relay
  tunnel. Trust only the run CA for this fixture. Keep the accepted browser
  helpers and their private credentials and create/verify receipts in that
  environment.
- Host the encrypted restic repository in separate protected operator
  storage. A pinned reverse SSH forward from each leased host's
  `127.0.0.1:22422` reaches the isolated operator rest-server loopback
  listener. Copy the root-owned mode-0400 repository password to
  `/var/lib/rehearsal-escrow/repository-password`. Backup and check timers
  remain disabled.
- Explicitly run `cloud-rehearsal-inputs.service` once on the original
  gateway, then use the [native rehearsal order](full-rehearsal.md) for
  migration, bootstrap, bounded browser/worker checks and quiescence. Set
  `SSL_CERT_FILE=/var/lib/terrace-rehearsal-trust/ca.pem` for the gateway's
  root-only Python protocol helper and use curl's explicit `--cacert`.
  After verifying the encrypted repository and escrow off both hosts, the
  provider receipt must establish that the originals are gone before the
  fresh restore. Transfer the retained TLS files independently and restore
  the application inputs before starting writers on the replacements.

The gateway contains the native rehearsal's pinned local PVC probe image,
`/etc/rehearsal-probe.yaml` and `rehearsal-cgroups-empty` for the same storage
and writer-stop checks.

The two-host profile covers the gateway and relay install configuration.
It leaves separate automation and peer-topology exercises to the full
native rehearsal or a later explicit cloud composition. The offline check
uses stub releases and evaluates role wiring. Actual provider installation,
private transport, application bootstrap, fresh recovery and resource
absence require the leased run.
