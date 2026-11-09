{
  config,
  lib,
  ...
}: let
  cfg = config.terrace.cloudDisk;
  byIdName = lib.removePrefix "/dev/disk/by-id/" cfg.device;
  isWholeDevice =
    builtins.match "^/dev/(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)$" cfg.device
    != null
    || (
      lib.hasPrefix "/dev/disk/by-id/" cfg.device
      && builtins.match "^[A-Za-z0-9][A-Za-z0-9_.:+-]*$" byIdName != null
      && builtins.match ".*-part[0-9]+.*" byIdName == null
    );
in {
  options.terrace.cloudDisk.device = lib.mkOption {
    type = lib.types.str;
    description = "Whole disposable Hetzner system disk, identified in Rescue before install.";
  };

  config = {
    assertions = [
      {
        assertion = isWholeDevice;
        message = "Terrace cloud disk requires a verified whole /dev device.";
      }
    ];
    # Hetzner's current server types boot UEFI. The pinned platform profile still
    # selects BIOS GRUB, so replace it with systemd-boot here. With no access to
    # firmware variables, bootctl installs the removable fallback
    # EFI/BOOT/BOOTX64.EFI, which a fresh disk boots without an NVRAM entry.
    boot.loader.grub.enable = lib.mkForce false;
    boot.loader.systemd-boot.enable = true;
    boot.loader.efi.canTouchEfiVariables = false;
    disko.devices.disk.system = {
      type = "disk";
      device = cfg.device;
      content = {
        type = "gpt";
        partitions = {
          ESP = {
            priority = 1;
            size = "512M";
            type = "EF00";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = ["umask=0077"];
            };
          };
          root = {
            size = "100%";
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/";
            };
          };
        };
      };
    };
  };
}
