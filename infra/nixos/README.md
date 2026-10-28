# Hetzner system disk profile

`flake.nix` exports `nixosModules.cloud-host`. It composes the pinned Disko
module with Terrace's K3s/SSH host profile and the BIOS/GPT disk layout in
`disk.nix`: a 1 MiB EF02 BIOS boot partition and an ext4 root partition.
The disk device is **required** and has no default. The build-only test uses
`/dev/vda` in a disposable VM; this says nothing about the future cloud disk.
The flake locks the platform module to Womb commit
`0349a5253018a0d4e8b0c04bdc47da39be77bad5`, so a future platform change
requires a reviewed pin update here.

Before any install, obtain a fresh disposable Terrace-project server, enter
Hetzner Rescue, and record `lsblk -o NAME,SIZE,MODEL,TYPE,SERIAL,PATH` and
`find /dev/disk/by-id -maxdepth 1 -type l`. Confirm the whole system disk,
its size and the server identity. Supply that whole disk to
`terrace.cloudDisk.device`; Disko destroys its contents. Never infer a live
disk from the VM test or an example. Apply only after a matching targeted
Pulumi preview, a project-scope check, and confirmed recovery access.
The module accepts canonical whole `sd`, `vd`, `xvd`, and `nvme` device names,
or a single `/dev/disk/by-id/` name without a `-partN` suffix. This syntax
check cannot prove where a by-id symlink points; verify that in Rescue.

For an actual fresh server, compose this module from a **local, uncommitted**
installation flake in Rescue. Its `nixpkgs` input should follow this flake's
locked `nixpkgs`; its NixOS configuration imports
`terrace.nixosModules.cloud-host`, a freshly generated
`hardware-configuration.nix` without filesystem declarations, and a local
module containing at least:

```nix
{
  networking.hostName = "terrace-bootstrap-1";
  networking.useDHCP = true; # verify against this server's network setup
  terrace.cloudDisk.device = "/dev/disk/by-id/REPLACE-WITH-VERIFIED-WHOLE-DISK";
  terrace.hetzner.authorizedKeys = ["ssh-ed25519 REPLACE-WITH-OPERATOR-PUBLIC-KEY"];
}
```

Evaluate the complete NixOS configuration and inspect its `fileSystems`,
`boot.loader.grub.device`, SSH settings and firewall before allowing Disko's
destructive `disko` mode. Then use the pinned Disko tooling to partition and
mount the verified disk and `nixos-install --flake` for that same configuration.
Reboot through Hetzner Console, verify operator-key SSH, K3s service and a
single Ready node, and verify port 6443 is unreachable from an external host.
The VM result below checks disk installation and boot; it does not replace
these cloud checks.

The install test runs Disko's NixOS installer in an isolated VM, boots the
result under BIOS/GRUB, and checks the root filesystem. It disables K3s only
inside that installer test; the separate `platform` VM test covers K3s. Run
only on a Linux builder under the shared heavy-build lock, with distributed
builders disabled:

```sh
: "${BUILD_LOCK:?set the shared build lock path}"
flock "$BUILD_LOCK" nix build .#checks.x86_64-linux.disk-install \
  --no-write-lock-file --max-jobs 1 --cores 2 \
  --option builders ""
```

Verified 2026-10-28 UTC on falcon: Disko's `installTest` passed through the
shared `falcon-heavy` lock (277 seconds including builds; 56 seconds for the
installer/boot VM). The VM installed to a disposable GPT disk, booted under
BIOS/GRUB, reached `multi-user.target`, and mounted an ext4 root. Result:
`/nix/store/x5gwjp50x41n989v4g54wn08lhsw90jq-vm-test-run-disko-terrace-disk-test-disko`.
This does not verify the target Hetzner firmware, disk name or network.
On the same host, Nix evaluation of the disk assertion accepted `/dev/vda`,
`/dev/nvme0n1`, and a whole-disk by-id name, and rejected `/dev/vda1`,
`/dev/nvme0n1p1`, and a by-id `-part1` name. No VM rebuild was needed for
this syntax-only assertion change.

The module is not an install command. It does not supply a disk identity,
network configuration, operator's real public key, or off-host backups.
For recovery, use Hetzner Console/Rescue independently of SSH on the installed
OS; restore the database, token and PVs from tested off-host backups before
switching traffic. Provider image backups alone do not prove database recovery.
