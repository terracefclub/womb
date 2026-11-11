# No builds, provider calls, credentials or guest state.
let
  install = builtins.getFlake "path:${toString ./.}";
  platform = builtins.getFlake "path:${toString ../../platform}";
  lib = platform.inputs.nixpkgs.lib;
  system = "x86_64-linux";
  pkgs = import platform.inputs.nixpkgs {inherit system;};
  localModule = {
    terrace.cloudDisk.device = "/dev/vda";
    terrace.hetzner.authorizedKeys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEoUtCnNlZBsJyHCk+HjpH1L4sIv9N4G3mFVL/+UuUYQ test-only"];
  };
  stub = pkgs.writeText "rehearsal-eval-only" "";
  gateway =
    (lib.nixosSystem {
      inherit system;
      modules = [
        install.nixosModules.cloud-disk
        localModule
        (import ../../platform/cloud-rehearsal/gateway.nix {
          release = {
            web = stub;
            cms = stub;
            revision = (builtins.fromJSON (builtins.readFile ../../platform/website-release.json)).revision;
          };
          contract = stub;
        })
      ];
    }).config;
  relayFor = privateIPv4:
    (import ./rehearsal-config.nix {
      inherit localModule privateIPv4;
      role = "relay";
    }).config;
  relay = relayFor "10.71.0.8";
  errors = cfg: map (a: a.message) (builtins.filter (a: !a.assertion) cfg.assertions);
  check = cfg:
    lib.assertMsg (errors cfg == []) (lib.concatStringsSep "\n" (errors cfg))
    && !cfg.boot.loader.grub.enable
    && cfg.boot.loader.systemd-boot.enable
    && !cfg.boot.loader.efi.canTouchEfiVariables
    && cfg.disko.devices.disk.system.device == "/dev/vda"
    && cfg.disko.devices.disk.system.content.partitions.ESP.type == "EF00"
    && lib.unique cfg.networking.firewall.allowedTCPPorts == [22]
    && cfg.networking.firewall.allowedUDPPorts == []
    && cfg.terrace.backup.targets.primary.repository == "rest:http://127.0.0.1:22422/repository"
    && cfg.terrace.backup.targets.primary.sftp.identityFile == null
    && cfg.systemd.timers.terrace-backup.wantedBy == [];
  noBoot = cfg: lib.all (name: cfg.systemd.services.${name}.wantedBy == []);
  trust = "/var/lib/terrace-rehearsal-trust/ca.pem";
in
  assert check gateway && check relay;
  assert gateway.services.k3s.enable && lib.versions.major (lib.getVersion gateway.services.postgresql.package) == "18";
  assert gateway.terrace.windmill.membershipRuntime && gateway.terrace.documenso.enableSigning;
  assert noBoot gateway ["postgresql" "nginx" "keycloak" "k3s" "terrace-web" "terrace-cms" "terrace-documenso" "vaultwarden" "podman-terrace-windmill-server" "podman-terrace-windmill-worker" "cloud-rehearsal-inputs"];
  assert gateway.systemd.targets.postgresql.wantedBy == [];
  assert builtins.elem (import ../../platform/full/probe-image.nix {inherit pkgs;}) gateway.services.k3s.images;
  assert gateway.environment.etc."rehearsal-probe.yaml".source == ../../platform/full/probe.yaml;
  assert lib.sort builtins.lessThan gateway.networking.firewall.interfaces.podman0.allowedTCPPorts == [443 1025];
  assert builtins.elem "${trust}:/run/terrace-ca.crt:ro" gateway.virtualisation.oci-containers.containers.terrace-windmill-server.volumes;
  assert gateway.terrace.documenso.identityCaFile == trust && gateway.terrace.vaultwarden.identityCaFile == trust;
  assert !relay.services.k3s.enable && relay.services.headscale.enable && relay.terrace.backup.headscale.enable;
  assert builtins.elem relay.services.headscale.package relay.environment.systemPackages;
  assert lib.all (name: lib.any (package: lib.getName package == name) relay.environment.systemPackages) ["curl" "jq" "sqlite"];
  assert noBoot relay ["headscale" "nginx" "cloud-rehearsal-relay-address"];
  assert relay.services.headscale.settings.derp.server.stun_listen_addr == "10.71.0.8:3478";
  assert relay.services.nginx.defaultListenAddresses == ["127.0.0.1"];
  assert relay.networking.firewall.interfaces == {};
  assert relay.services.nginx.virtualHosts."relay.terrace.invalid".onlySSL && !relay.services.nginx.virtualHosts."relay.terrace.invalid".enableACME;
  # nginx runs as its own user, so the root-only relay leaf must arrive as a systemd credential.
  assert relay.services.nginx.virtualHosts."relay.terrace.invalid".sslCertificateKey == "/run/credentials/nginx.service/terrace-relay-key";
  assert relay.systemd.services.nginx.serviceConfig.LoadCredential == ["terrace-relay-cert:/var/lib/terrace-rehearsal-tls/cert.pem" "terrace-relay-key:/var/lib/terrace-rehearsal-tls/key.pem"];
  assert builtins.elem "The synthetic relay requires a private RFC1918 IPv4 address." (errors (relayFor "203.0.113.10"));
  assert lib.all (cfg: builtins.isString cfg.system.build.toplevel.drvPath && builtins.isString cfg.system.build.diskoScript.drvPath) [gateway relay]; "PASS: cloud synthetic roles keep startup, ingress, TLS inputs and backup off-host explicit"
