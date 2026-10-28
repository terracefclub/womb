{
  config,
  lib,
  ...
}: let
  cfg = config.terrace.hetzner;
in {
  imports = [./single-node.nix];

  options.terrace.hetzner = {
    enable = lib.mkEnableOption "Terrace Hetzner BIOS host profile";
    authorizedKeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Operator SSH public keys supplied outside this repository.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.authorizedKeys != [];
        message = "Terrace Hetzner profile requires at least one operator SSH public key.";
      }
    ];

    terrace.singleNode.enable = true;
    system.stateVersion = "26.05";
    boot.loader.grub = {
      enable = true;
      efiSupport = false;
      # Evaluation only: replace with a verified disk path for any install.
      device = "nodev";
    };
    services.openssh = {
      enable = true;
      settings = {
        PermitRootLogin = "no";
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
      };
    };
    users.users.operator = {
      isNormalUser = true;
      extraGroups = ["wheel"];
      openssh.authorizedKeys.keys = cfg.authorizedKeys;
    };
    security.sudo.wheelNeedsPassword = false;
    networking.firewall.allowedTCPPorts = [22];
  };
}
