{
  release,
  contract,
}: {
  config,
  lib,
  pkgs,
  ...
}: let
  identity = "identity.terrace.invalid";
  web = "web.terrace.invalid";
  cms = "cms.terrace.invalid";
  documents = "documents.terrace.invalid";
  windmill = "windmill.terrace.invalid";
  vault = "vault.terrace.invalid";
  names = [identity web cms documents windmill vault];
  inputs = "/var/lib/terrace-gateway-test";
  trust = "/var/lib/terrace-rehearsal-trust/ca.pem";
  tls = {
    certificateFile = "/var/lib/terrace-rehearsal-tls/cert.pem";
    certificateKeyFile = "/var/lib/terrace-rehearsal-tls/key.pem";
  };
  fixture = import ../full/fixture-package.nix {inherit pkgs contract;};
  writers = [
    "postgresql"
    "keycloak"
    "nginx"
    "k3s"
    "terrace-web"
    "terrace-cms"
    "terrace-documenso"
    "podman-terrace-windmill-server"
    "podman-terrace-windmill-worker"
    "vaultwarden"
  ];
in {
  imports = [
    (import ./common.nix {tlsNames = names;})
    ../hetzner.nix
    ../website-gateway.nix
    ../identity.nix
    ../documenso.nix
    ../documenso/mailpit-test.nix
    ../windmill.nix
    ../vaultwarden.nix
  ];
  networking.hostName = "gateway";
  networking.hosts."127.0.0.1" = names;
  # Documenso resolves the issuer via its own Podman host gateway.
  networking.firewall.interfaces.podman0.allowedTCPPorts = [443];
  terrace.hetzner.enable = true;
  terrace.website = {
    enable = true;
    webPackage = release.web;
    cmsPackage = release.cms;
    inherit (release) revision;
  };
  terrace.websiteGateway =
    tls
    // {
      enable = true;
      webHostname = web;
      cmsHostname = cms;
    };
  terrace.identity =
    tls
    // {
      enable = true;
      hostname = identity;
      realmFile = "${inputs}/terrace-realm.json";
      databasePasswordFile = "${inputs}/db-password";
    };
  terrace.documenso =
    tls
    // {
      enable = true;
      enableSigning = true;
      hostname = documents;
      identityHostname = identity;
      identityCaFile = trust;
    };
  terrace.windmill =
    tls
    // {
      enable = true;
      membershipRuntime = true;
      hostname = windmill;
      identityCaFile = trust;
    };
  terrace.vaultwarden =
    tls
    // {
      enable = true;
      hostname = vault;
      identityHostname = identity;
      identityCaFile = trust;
    };
  services.postgresql = {
    package = lib.mkForce pkgs.postgresql_18;
    settings = {
      max_connections = lib.mkForce 64;
      shared_buffers = lib.mkForce "128MB";
      work_mem = "2MB";
    };
  };
  services.k3s.images = [pkgs.k3s.airgap-images];
  systemd.services = lib.mkMerge [
    (lib.genAttrs writers (_: {wantedBy = lib.mkForce [];}))
    (lib.genAttrs ["terrace-cms" "terrace-cms-migrate" "terrace-web"] (_: {
      environment.NODE_EXTRA_CA_CERTS = trust;
    }))
    {
      postgresql.serviceConfig.MemoryMax = lib.mkForce "1536M";
      keycloak.serviceConfig.EnvironmentFile = "${inputs}/keycloak.env";
      cloud-rehearsal-inputs = {
        # Explicit original-only fixture generation. Replacement inputs come from restic.
        unitConfig.ConditionPathExists = "!${inputs}/terrace-realm.json";
        path = [pkgs.openssl];
        serviceConfig = {
          Type = "oneshot";
          UMask = "0077";
          TimeoutStartSec = 120;
          StandardOutput = "null";
          StandardError = "null";
        };
        script = "exec ${pkgs.bun}/bin/bun --no-env-file ${fixture}/full/fixture.ts";
      };
      keycloakPostgreSQLInit.postStart = ''
        psql -X -w -v ON_ERROR_STOP=1 -d postgres -c 'ALTER ROLE keycloak NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10; ALTER ROLE terrace_cms CONNECTION LIMIT 10;' >/dev/null 2>&1
      '';
    }
  ];
  systemd.targets.postgresql.wantedBy = lib.mkForce [];
  systemd.timers.terrace-identity-health.wantedBy = lib.mkForce [];
  terrace.backup = {
    postgresql.databases = ["keycloak"];
    extraPaths = {
      identity-inputs = inputs;
      k3s-server = "/var/lib/rancher/k3s/server";
      k3s-storage = "/var/lib/rancher/k3s/storage";
      k3s-node = "/etc/rancher/node";
    };
  };
  environment.systemPackages = [pkgs.curl pkgs.jq pkgs.python3 pkgs.openssl pkgs.netcat-openbsd];
  environment.etc = {
    "rehearsal-pg-directory".text = config.services.postgresql.dataDir;
    "gateway-protocol.py".source = ../website-gateway/protocol.py;
    "documenso-recovery.py".source = ../documenso/recovery-auth-test.py;
    "documenso-signing-proof.py".source = ../documenso/signing-proof.py;
    "documenso-smtp-probe.cjs".source = ../documenso/smtp-probe.cjs;
  };
}
