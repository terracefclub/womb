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
    # Disko's EF02 partition already adds the disk to grub.devices. Clear the
    # host profile's singular fallback so GRUB does not add a second device.
    boot.loader.grub.device = lib.mkForce "";
    disko.devices.disk.system = {
      type = "disk";
      device = cfg.device;
      content = {
        type = "gpt";
        partitions = {
          bios = {
            size = "1M";
            type = "EF02";
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
