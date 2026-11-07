# Public installer image. The pinned runner transfers the SSH files at runtime.
let
  images = builtins.getFlake "github:nix-community/nixos-images/50939ddf1a0339f289a9dbd810bdfd4e4fba6df4";
  # Use upstream's locked NixOS 26.05 for this ephemeral installer only.
  # The application closure and Disko retain the repository's own pins.
  nixpkgs = images.inputs.nixos-stable;
  installer = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      images.nixosModules.kexec-installer
      ({
        lib,
        pkgs,
        ...
      }: {
        # The disposable GPT/ext4 target needs no ZFS-specific kernel.
        boot.kernelPackages = lib.mkForce pkgs.linuxPackages;
        boot.supportedFilesystems = lib.mkForce ["ext4" "vfat" "squashfs" "overlay"];
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
  pkgs = nixpkgs.legacyPackages.x86_64-linux;
  ssh = config.services.openssh;
  # The upstream runner calls this only on Debian, before entering NixOS.
  # Use Debian's iproute2 instead of building a second static iproute2.
  stockIp = pkgs.writeText "stock-debian-ip" ''
    #!/bin/sh
    set -eu
    test -f /usr/bin/ip && test -x /usr/bin/ip
    test "$(stat -Lc '%u:%g:%a' /usr/bin/ip)" = 0:0:755
    exec /usr/bin/ip "$@"
  '';
  archive = pkgs.runCommand "terrace-kexec-tarball" {} ''
    mkdir kexec "$out"
    install -m 0644 "${config.system.build.netbootRamdisk}/initrd" kexec/initrd
    install -m 0644 "${config.system.build.kernel}/${config.system.boot.loader.kernelFile}" kexec/bzImage
    install -m 0755 "${config.system.build.kexecRun}" kexec/run
    install -m 0755 "${pkgs.pkgsStatic.kexec-tools}/bin/kexec" kexec/kexec
    install -m 0755 "${stockIp}" kexec/ip
    tar -czf "$out/nixos-kexec-installer-x86_64-linux.tar.gz" kexec
  '';
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
