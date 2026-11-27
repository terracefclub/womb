# Hetzner Cloud hardware for a production host (docs/production-profile.md,
# "Hardware module interface"): the virtio drivers and DHCP networking, nothing
# else. Import it together with the cloud disk module (`nixosModules.cloud-disk`:
# GPT, ESP, ext4, systemd-boot on the removable fallback path) and set
# `terrace.cloudDisk.device` to the whole disk the Rescue probe verified; the
# flake export `nixosModules.hetzner-production-hardware` is that pair.
#
# Leases 3 and 5 installed and booted the rehearsal profile on Hetzner CPX
# servers with exactly this hardware part (the QEMU guest profile's virtio
# drivers and DHCP on every interface, UEFI, `/dev/sda`). The production
# profile has not yet booted on a provider.
{modulesPath, ...}: {
  # virtio-net, virtio-scsi (Hetzner's `/dev/sda`), virtio-pci and the other
  # QEMU guest drivers in the initrd. NixOS's default initrd modules (sd_mod,
  # sr_mod, ahci, xhci_pci) stay as they are.
  imports = [(modulesPath + "/profiles/qemu-guest.nix")];
  # The address comes from Hetzner's DHCP on whatever the virtio NIC is called;
  # the interface name is not pinned because the production closure has never
  # been observed on the provider. The evaluation check accepts dhcpcd at boot.
  networking.useDHCP = true;
}
