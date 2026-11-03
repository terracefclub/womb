{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.identity;
  runtimePath = path: lib.hasPrefix "/" path && !(lib.hasPrefix "/nix/store" path);
  proxy = {
    proxyPass = "http://127.0.0.1:8088";
    recommendedProxySettings = false;
    extraConfig = ''
      proxy_set_header Host ${cfg.hostname};
      proxy_set_header X-Forwarded-Host ${cfg.hostname};
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Port 443;
      proxy_set_header X-Forwarded-For $remote_addr;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header Forwarded "";
      proxy_set_header X-Forwarded-Prefix "";
      proxy_connect_timeout 5s;
      proxy_read_timeout 30s;
      proxy_send_timeout 30s;
    '';
  };
in {
  options.terrace.identity = {
    enable = lib.mkEnableOption "single-node Terrace identity host";
    hostname = lib.mkOption {
      type = lib.types.str;
      description = "Exact public DNS hostname for the HTTPS issuer; no DNS is provisioned.";
    };
    realmFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime-only, user-free terrace-realm.json built from the reviewed TFC identity contract; never a local fixture export.";
    };
    databasePasswordFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime-only PostgreSQL password file, delivered by the secret manager.";
    };
    certificateFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime PEM certificate chain for hostname; no ACME account is created.";
    };
    certificateKeyFile = lib.mkOption {
      type = lib.types.str;
      description = "Runtime PEM private key; readable only by root.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.match "[a-zA-Z0-9][a-zA-Z0-9.-]*" cfg.hostname != null;
        message = "Terrace identity hostname must be a DNS name, not a URL.";
      }
      {
        assertion = lib.all runtimePath [cfg.realmFile cfg.databasePasswordFile cfg.certificateFile cfg.certificateKeyFile];
        message = "Terrace identity inputs must be absolute runtime file paths outside the Nix store.";
      }
      {
        assertion = config.services.keycloak.initialAdminPassword == null;
        message = "Terrace identity must not seed a shared bootstrap administrator.";
      }
    ];

    services.keycloak = {
      enable = true;
      database = {
        type = "postgresql";
        host = "localhost";
        createLocally = true;
        useSSL = false;
        passwordFile = cfg.databasePasswordFile;
      };
      realmFiles = ["/run/credentials/keycloak.service/terrace-realm.json"];
      settings = {
        hostname = "https://${cfg.hostname}";
        hostname-strict = true;
        hostname-backchannel-dynamic = false;
        http-enabled = true;
        http-host = "127.0.0.1";
        http-port = 8088;
        proxy-headers = "xforwarded";
        proxy-trusted-addresses = "127.0.0.1";
        http-management-host = "127.0.0.1";
        http-management-port = 9000;
        health-enabled = true;
        metrics-enabled = true;
        cache = "local";
        db-pool-initial-size = 1;
        db-pool-min-size = 1;
        db-pool-max-size = 10;
        http-pool-max-threads = 32;
        http-access-log-enabled = false;
        log = "file";
        log-level = "warn";
        log-file = "/run/keycloak/keycloak.log";
        log-file-rotation-enabled = true;
        log-file-rotation-max-file-size = "5M";
        log-file-rotation-max-backup-index = 2;
        log-file-rotation-rotate-on-boot = true;
      };
    };
    services.postgresql.settings = {
      listen_addresses = lib.mkDefault "127.0.0.1";
      max_connections = lib.mkDefault 40;
      shared_buffers = lib.mkDefault "64MB";
    };
    systemd.services.keycloakPostgreSQLInit.serviceConfig = {
      StandardOutput = "null";
      StandardError = "null";
    };
    systemd.services.keycloak = {
      environment.JAVA_OPTS_APPEND = "-Xms256m -Xmx768m -XX:MaxMetaspaceSize=256m";
      preStart = ''
        # Parse only the protected systemd credential, never at Nix evaluation time.
        if ! test "$(stat -c %s "$CREDENTIALS_DIRECTORY/terrace-realm.json")" -le 1048576 ||
          ! ${pkgs.jq}/bin/jq -e -f ${./identity/realm-policy.jq} \
            "$CREDENTIALS_DIRECTORY/terrace-realm.json" >/dev/null 2>&1; then
          echo 'Terrace realm input rejected; check the documented production artifact policy.' >&2
          exit 1
        fi
      '';
      # The upstream notify plugin can announce startup before schema migration finishes.
      postStart = ''
        set -o pipefail
        ${pkgs.curl}/bin/curl --fail --silent --connect-timeout 2 --max-time 5 \
          --retry 60 --retry-connrefused --retry-delay 2 --retry-max-time 240 \
          http://127.0.0.1:9000/health/ready 2>/dev/null |
          ${pkgs.jq}/bin/jq -e '.status == "UP"' >/dev/null 2>&1
      '';
      unitConfig = {
        StartLimitIntervalSec = 600;
        StartLimitBurst = 3;
      };
      serviceConfig = {
        LoadCredential = ["terrace-realm.json:${cfg.realmFile}"];
        MemoryHigh = "1536M";
        MemoryMax = "2G";
        CPUQuota = "200%";
        TasksMax = 256;
        TimeoutStartSec = 300;
        TimeoutStopSec = 60;
        Restart = "on-failure";
        RestartSec = 10;
        # Import/startup errors can contain protected realm/client data.
        StandardOutput = "null";
        StandardError = "null";
        UMask = "0077";
      };
    };
    systemd.services.postgresql.serviceConfig = {
      MemoryMax = lib.mkDefault "1G";
      CPUQuota = lib.mkDefault "200%";
    };

    services.nginx = {
      enable = true;
      recommendedTlsSettings = true;
      virtualHosts.${cfg.hostname} = {
        onlySSL = true;
        sslCertificate = "/run/credentials/nginx.service/terrace-identity-cert";
        sslCertificateKey = "/run/credentials/nginx.service/terrace-identity-key";
        extraConfig = ''
          access_log off;
          client_max_body_size 1m;
        '';
        locations = {
          "/".return = "404";
          "/realms/terrace/" = proxy;
          "/realms/terrace/clients-registrations".return = "404";
          "/resources/" = proxy;
          "= /.well-known/oauth-authorization-server/realms/terrace" = proxy;
        };
      };
    };
    systemd.services.nginx.serviceConfig.LoadCredential = [
      "terrace-identity-cert:${cfg.certificateFile}"
      "terrace-identity-key:${cfg.certificateKeyFile}"
    ];
    networking.firewall.allowedTCPPorts = [443];

    systemd.services.terrace-identity-health = {
      description = "Check local Keycloak database readiness";
      after = ["keycloak.service"];
      serviceConfig = {
        Type = "oneshot";
        DynamicUser = true;
        TimeoutStartSec = 10;
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
      };
      script = ''
        set -o pipefail
        ${pkgs.curl}/bin/curl --fail --silent --connect-timeout 2 --max-time 5 \
          http://127.0.0.1:9000/health/ready 2>/dev/null |
          ${pkgs.jq}/bin/jq -e '.status == "UP"' >/dev/null 2>&1 || {
          echo 'Terrace identity readiness failed; check the local service and database.' >&2
          exit 1
        }
      '';
    };
    systemd.timers.terrace-identity-health = {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "1min";
      };
    };
  };
}
