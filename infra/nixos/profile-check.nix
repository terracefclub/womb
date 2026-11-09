# Evaluate pinned install modules and the current app/relay roles without a build.
let
  install = builtins.getFlake "path:${toString ./.}";
  platform = builtins.getFlake "path:${toString ../../platform}";
  lib = platform.inputs.nixpkgs.lib;
  disk = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_terrace-eval-only";
  operatorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEoUtCnNlZBsJyHCk+HjpH1L4sIv9N4G3mFVL/+UuUYQ test-only";
  localConfig = {
    networking.hostName = "terrace-profile-check";
    terrace.cloudDisk.device = disk;
    terrace.hetzner.authorizedKeys = [operatorKey];
  };
  evaluate = modules:
    (lib.nixosSystem {
      system = "x86_64-linux";
      modules = [localConfig] ++ modules;
    }).config;
  legacy = evaluate [install.nixosModules.cloud-host];
  current = [
    install.nixosModules.cloud-disk
    platform.nixosModules.hetzner
    platform.nixosModules.relay
    {terrace.hetzner.enable = true;}
  ];
  app = evaluate current;
  relay = evaluate (current
    ++ [
      {
        terrace.hetzner.enableK3s = false;
        terrace.relay = {
          enable = true;
          hostname = "relay.terrace.invalid";
          publicIPv4 = "192.0.2.10";
        };
        services.nginx.virtualHosts."relay.terrace.invalid" = {
          enableACME = lib.mkForce false;
          sslCertificate = "/run/terrace-test/cert.pem";
          sslCertificateKey = "/run/terrace-test/key.pem";
        };
      }
    ]);
  systemValid = cfg: let
    failures = builtins.filter (entry: !entry.assertion) cfg.assertions;
  in
    lib.assertMsg (failures == []) (lib.concatMapStringsSep "\n" (entry: entry.message) failures);
  bootAndSSH = cfg: let
    loader = cfg.boot.loader;
    layout = cfg.disko.devices.disk.system;
    esp = layout.content.partitions.ESP;
    ssh = cfg.services.openssh;
  in
    !loader.grub.enable
    && loader.systemd-boot.enable
    && !loader.efi.canTouchEfiVariables
    && layout.device == disk
    && layout.content.type == "gpt"
    && lib.attrNames layout.content.partitions == ["ESP" "root"]
    && esp.type == "EF00"
    && esp.content.format == "vfat"
    && esp.content.mountpoint == "/boot"
    && cfg.fileSystems."/boot".fsType == "vfat"
    && layout.content.partitions.root.content.format == "ext4"
    && layout.content.partitions.root.content.mountpoint == "/"
    && ssh.enable
    && ssh.settings.PermitRootLogin == "no"
    && !ssh.settings.PasswordAuthentication
    && !ssh.settings.KbdInteractiveAuthentication
    && cfg.users.users.operator.openssh.authorizedKeys.keys == [operatorKey]
    && cfg.networking.firewall.enable
    && !(builtins.elem 6443 cfg.networking.firewall.allowedTCPPorts);
in
  assert bootAndSSH legacy && legacy.services.k3s.enable;
  assert bootAndSSH app && app.services.k3s.enable && !app.services.headscale.enable;
  assert bootAndSSH relay && !relay.services.k3s.enable && relay.services.headscale.enable;
  assert systemValid legacy && systemValid app && systemValid relay;
  assert relay.services.headscale.address == "127.0.0.1";
  assert lib.sort builtins.lessThan (lib.unique relay.networking.firewall.allowedTCPPorts) == [22 80 443];
  assert lib.unique relay.networking.firewall.allowedUDPPorts == [3478]; "PASS: legacy and current app install profiles plus independent relay"
