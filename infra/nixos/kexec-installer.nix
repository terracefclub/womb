# Public installer image. The pinned runner transfers the SSH files at runtime.
let
  nixpkgs = (builtins.getFlake "path:${toString ./.}").inputs.nixpkgs;
  images = builtins.getFlake "github:nix-community/nixos-images/aa38dbbdf0e955baef7e03dfc4265ae3fdac4808";
  installer = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      images.nixosModules.kexec-installer
      ({
        lib,
        pkgs,
        ...
      }: {
        # This GPT/ext4 installer needs no ZFS-specific kernel. The old upstream
        # installer module names a ZFS package alias removed by our nixpkgs pin.
        boot.kernelPackages = lib.mkForce pkgs.linuxPackages;
        boot.supportedFilesystems = lib.mkForce ["ext4" "vfat" "squashfs" "overlay"];
        boot.zfs.package = lib.mkForce pkgs.zfs;
        nix.settings.substituters = lib.mkForce [];
        nix.settings.extra-substituters = lib.mkForce [];
        networking.hostName = "terrace-ram-installer";
        networking.firewall.enable = lib.mkForce true;
        networking.firewall.allowedTCPPorts = [22];
        networking.firewall.allowedUDPPorts = lib.mkForce [];
        environment.etc."terrace-ram-installer".text = "terrace-ram-installer-v1";
        services.openssh = {
          enable = true;
          authorizedKeysFiles = lib.mkForce ["/root/.ssh/authorized_keys"];
          hostKeys = [
            {
              path = "/etc/ssh/ssh_host_ed25519_key";
              type = "ed25519";
            }
          ];
          settings = {
            AllowUsers = ["root"];
            PasswordAuthentication = false;
            KbdInteractiveAuthentication = false;
            PermitRootLogin = "prohibit-password";
          };
        };
      })
    ];
  };
  inherit (installer) config;
  ssh = config.services.openssh;
  archive = config.system.build.kexecInstallerTarball;
in {
  inherit archive;
  proof = assert config.boot.initrd.systemd.enable;
  assert config.fileSystems."/".fsType == "tmpfs";
  assert config.fileSystems."/nix/.ro-store".fsType == "squashfs";
  assert config.fileSystems."/nix/.rw-store".fsType == "tmpfs";
  assert config.fileSystems."/nix/store".overlay != null;
  assert ssh.enable && ssh.settings.AllowUsers == ["root"];
  assert !ssh.settings.PasswordAuthentication && !ssh.settings.KbdInteractiveAuthentication;
  assert ssh.settings.PermitRootLogin == "prohibit-password";
  assert ssh.authorizedKeysFiles == ["/root/.ssh/authorized_keys"];
  assert map (key: [key.type key.path]) ssh.hostKeys == [["ed25519" "/etc/ssh/ssh_host_ed25519_key"]];
  assert config.networking.firewall.enable;
  assert nixpkgs.lib.unique config.networking.firewall.allowedTCPPorts == [22];
  assert config.networking.firewall.allowedUDPPorts == []; {
    archiveDrv = archive.drvPath;
    nixStore = "${config.nix.package}/bin/nix-store";
  };
}
