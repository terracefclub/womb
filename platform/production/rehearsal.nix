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
  stage = "/tmp/shared/stage/${config.networking.hostName}";
in {
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
    # Stand-in for the Rescue installer: copy the operator-generated host key and
    # the ciphertext staged for this host before secrets are installed.
    terrace-rehearsal-stage = {
      description = "Rehearsal installer stand-in: stage the host key and encrypted secrets";
      wantedBy = ["sysinit.target"];
      before = ["sops-install-secrets.service" "sshd.service" "sysinit.target"];
      after = ["local-fs.target"];
      unitConfig = {
        DefaultDependencies = false;
        RequiresMountsFor = ["/tmp/shared" "/var/lib"];
      };
      path = [pkgs.coreutils];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        UMask = "0077";
      };
      script = ''
        set -euo pipefail
        if test -f ${stage}/ssh_host_ed25519_key && test ! -e /etc/ssh/ssh_host_ed25519_key; then
          install -d -m 0755 /etc/ssh
          install -m 0600 ${stage}/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key
          install -m 0644 ${stage}/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ed25519_key.pub
        fi
        if test -f ${stage}/secrets.sops.json && test ! -e /var/lib/terrace-secrets/secrets.sops.json; then
          install -d -m 0700 /var/lib/terrace-secrets
          install -m 0600 ${stage}/secrets.sops.json /var/lib/terrace-secrets/secrets.sops.json
        fi
      '';
    };
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
