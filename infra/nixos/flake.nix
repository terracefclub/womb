{
  description = "Terrace disposable Hetzner host disk and boot profile";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/31ecbaca1bdefa45dffc409f0250bd8039ea0010";
    disko.url = "github:nix-community/disko";
    disko.inputs.nixpkgs.follows = "nixpkgs";
    platform.url = "git+https://gitlab.com/terrace/tech/womb.git?dir=platform&rev=0349a5253018a0d4e8b0c04bdc47da39be77bad5";
    platform.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = {
    nixpkgs,
    disko,
    platform,
    ...
  }: let
    system = "x86_64-linux";
    diskModule = {...}: {
      imports = [
        disko.nixosModules.disko
        ./disk.nix
      ];
    };
    hostModule = {...}: {
      imports = [diskModule platform.nixosModules.hetzner];
      terrace.hetzner.enable = true;
    };
    installConfig = nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [
        hostModule
        ({lib, ...}: {
          networking.hostName = "terrace-disk-test";
          terrace.hetzner.authorizedKeys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEoUtCnNlZBsJyHCk+HjpH1L4sIv9N4G3mFVL/+UuUYQ test-only"];
          terrace.cloudDisk.device = "/dev/vda";
          disko.tests.extraConfig.services.k3s.enable = lib.mkForce false;
          # systemd-boot makes Disko boot this test under OVMF with read-only
          # variables, so the installed disk must start from its fallback path.
          disko.tests.extraChecks = ''
            machine.wait_for_unit("multi-user.target")
            machine.succeed("findmnt -no FSTYPE / | grep -qx ext4")
            machine.succeed("test -d /sys/firmware/efi")
            machine.succeed("findmnt -no FSTYPE /boot | grep -qx vfat")
            machine.succeed("test -f /boot/EFI/BOOT/BOOTX64.EFI")
            machine.succeed("bootctl is-installed")
          '';
        })
      ];
    };
  in {
    nixosModules.cloud-disk = diskModule;
    nixosModules.cloud-host = hostModule;
    checks.${system}.disk-install = installConfig.config.system.build.installTest;
  };
}
