# Production access relay: Headscale with embedded DERP behind nginx, an
# independent host so recovery access survives an application-host outage.
{
  config,
  lib,
  ...
}: let
  cfg = config.terrace.production;
  relay = cfg.relay;
  secret = name: config.terrace.production.secrets.path.${name};
  octets = map lib.toInt (lib.splitString "." relay.publicIPv4);
  octet = builtins.elemAt octets;
  nonPublic =
    octet 0
    == 0
    || octet 0 == 10
    || octet 0 == 127
    || octet 0 >= 224
    || (octet 0 == 100 && octet 1 >= 64 && octet 1 <= 127)
    || (octet 0 == 169 && octet 1 == 254)
    || (octet 0 == 172 && octet 1 >= 16 && octet 1 <= 31)
    || (octet 0 == 192 && octet 1 == 168)
    || (octet 0 == 198 && (octet 1 == 18 || octet 1 == 19))
    # Documentation ranges (RFC 5737): placeholders only, accepted by the rehearsal instance alone.
    || (octet 0 == 192 && octet 1 == 0 && octet 2 == 2)
    || (octet 0 == 198 && octet 1 == 51 && octet 2 == 100)
    || (octet 0 == 203 && octet 1 == 0 && octet 2 == 113);
in {
  imports = [
    ./base.nix
    ./secrets.nix
    ./tls.nix
    ../relay.nix
    ../backup.nix
  ];
  options.terrace.production.relay = {
    hostname = lib.mkOption {
      type = lib.types.str;
      description = "Required: public relay hostname (direct DNS, no CDN).";
    };
    publicIPv4 = lib.mkOption {
      type = lib.types.str;
      description = "Required: the relay's verified public IPv4 (DERP advertisement).";
    };
    backup.primary = {
      repository = lib.mkOption {
        type = lib.types.str;
        description = "Required: restic SFTP repository for the relay (its own, separate from the app's).";
      };
      knownHosts = lib.mkOption {
        type = lib.types.str;
        description = "Required: known_hosts line(s) pinning the SFTP server.";
      };
      port = lib.mkOption {
        type = lib.types.port;
        default = 22;
      };
    };
  };
  config = {
    assertions = [
      {
        assertion = builtins.match "(0|[1-9][0-9]{0,2})([.](0|[1-9][0-9]{0,2})){3}" relay.publicIPv4 != null && lib.all (n: n <= 255) octets && (cfg.instance != "production" || !nonPublic);
        message = "Terrace production relay needs its verified public IPv4 (no private, shared, loopback, link-local, benchmarking or documentation range).";
      }
      {
        assertion = lib.hasPrefix "sftp:" relay.backup.primary.repository;
        message = "Terrace production relay backup needs an sftp: repository and a pinned known_hosts entry.";
      }
      {
        assertion = lib.all (path: lib.hasPrefix "/run/secrets/" path) (
          map (entry: lib.last (lib.splitString ":" entry)) config.systemd.services.nginx.serviceConfig.LoadCredential
          ++ lib.concatMap (target: [target.passwordFile] ++ lib.optional (target.environmentFile != null) target.environmentFile ++ lib.optional (target.sftp.identityFile != null) target.sftp.identityFile)
          (lib.attrValues (lib.filterAttrs (_: target: target.enable) config.terrace.backup.targets))
        );
        message = "Terrace production secret consumers must read sops-nix paths under /run/secrets, nothing else.";
      }
      {
        assertion = config.services.headscale.address == "127.0.0.1" && lib.hasPrefix "127.0.0.1:" config.services.headscale.settings.metrics_listen_addr && lib.hasPrefix "127.0.0.1:" config.services.headscale.settings.grpc_listen_addr;
        message = "Terrace production keeps Headscale's API, metrics and gRPC on loopback.";
      }
    ];
    terrace.production = {
      enable = true;
      role = "relay";
      publicHostnames = [relay.hostname];
      # Port 80: the relay module's HTTPS redirect, or with ACME the challenge and redirect (tls.nix).
      publicTCPPorts = [22 80 443];
      publicUDPPorts = [3478];
      writers = ["headscale" "nginx"];
      stateWitness = "/var/lib/headscale/db.sqlite";
      writerTimers = ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness"];
    };
    terrace.relay = {
      enable = true;
      inherit (relay) hostname publicIPv4;
    };
    # The relay module's own ACME is never used: tls.nix selects the operator's
    # certificate (manual, below; the module's port-80 redirect stays) or its ACME wiring.
    services.nginx.virtualHosts.${relay.hostname} = {
      enableACME = lib.mkForce false;
      sslCertificate = lib.mkIf (cfg.tls.mode == "manual") "/run/credentials/nginx.service/terrace-relay-cert";
      sslCertificateKey = lib.mkIf (cfg.tls.mode == "manual") "/run/credentials/nginx.service/terrace-relay-key";
    };
    systemd.services.nginx.serviceConfig = {
      LoadCredential = lib.mkIf (cfg.tls.mode == "manual") ["terrace-relay-cert:${secret "tls/certificate"}" "terrace-relay-key:${secret "tls/key"}"];
      MemoryMax = "256M";
    };
    systemd.services.headscale.serviceConfig.MemoryMax = "512M";
    environment.etc."terrace/backup/primary-known-hosts".text = relay.backup.primary.knownHosts + "\n";
    terrace.backup = {
      enable = true;
      targets.primary = {
        enable = true;
        inherit (relay.backup.primary) repository;
        passwordFile = secret "backup/primary/repository-password";
        sftp = {
          identityFile = secret "backup/primary/sftp-key";
          knownHostsFile = "/etc/terrace/backup/primary-known-hosts";
          inherit (relay.backup.primary) port;
        };
      };
    };
  };
}
