# Rehearsal-only harness for the VM proof of the production app role. It adds
# only what the shared gateway protocol needs and the installer stand-in; the
# production profile refuses it (terrace.production.rehearsalHarness).
{ca}: {
  config,
  lib,
  pkgs,
  ...
}: let
  trust = "${ca}/ca.pem";
  placeholder = name: config.sops.placeholder.${name};
in {
  imports = [./rehearsal-stage.nix];
  terrace.production.rehearsalHarness = true;
  # Synthetic CA for the .invalid names (the protocol client and the CMS/website
  # server-to-server calls trust it); production uses public certificates.
  security.pki.certificateFiles = [trust];
  systemd.services = {
    terrace-cms.environment.NODE_EXTRA_CA_CERTS = trust;
    terrace-cms-migrate.environment.NODE_EXTRA_CA_CERTS = trust;
    terrace-cms-sweep.environment.NODE_EXTRA_CA_CERTS = trust;
    terrace-web.environment.NODE_EXTRA_CA_CERTS = trust;
    # The protocol enrols synthetic users through a temporary master
    # administrator. Keycloak sees it only while the test copies the rendered
    # file into /run (first start); the protocol deletes the account.
    keycloak.serviceConfig.EnvironmentFile = "-/run/terrace-rehearsal/keycloak-bootstrap.env";
  };
  sops.secrets = {
    "identity/bootstrap-admin-password".mode = "0400";
    "rehearsal/users-json".mode = "0400";
  };
  sops.templates = {
    "rehearsal/keycloak-bootstrap.env".content = ''
      KC_BOOTSTRAP_ADMIN_USERNAME=gateway-fixture-bootstrap
      KC_BOOTSTRAP_ADMIN_PASSWORD=${placeholder "identity/bootstrap-admin-password"}
    '';
    # The shared protocol reads its synthetic accounts from this fixed path.
    "rehearsal/credentials.json" = {
      path = "/var/lib/terrace-gateway-test/credentials.json";
      content = ''{"adminPassword": "${placeholder "identity/bootstrap-admin-password"}", "users": ${placeholder "rehearsal/users-json"}}'';
    };
  };
  environment.etc = {
    "gateway-protocol.py".source = ../website-gateway/protocol.py;
    "gateway-content.sql".source = ../website-gateway/content.sql;
    "terrace-rehearsal-ca.pem".source = trust;
    "rehearsal-pg-directory".text = config.services.postgresql.dataDir;
    # Positive control for the closure scan: a known non-secret string in the system closure.
    "terrace-closure-canary".text = "terrace-closure-canary-5b1d0c7e9a3f4c28b6e1d2a7f0c9e8b4\n";
  };
  environment.systemPackages = [
    pkgs.curl
    pkgs.jq
    pkgs.python3
    pkgs.netcat-openbsd
    pkgs.postgresql_18
    pkgs.iproute2
    (pkgs.writers.writePython3Bin "rehearsal-patterns" {flakeIgnore = ["E501"];} (builtins.readFile ./rehearsal-patterns.py))
  ];
}
