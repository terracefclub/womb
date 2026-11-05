{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.vaultwarden;
  release = import ./vaultwarden/release.nix {inherit pkgs;};
  role = "terrace_vaultwarden";
  origin = "https://${cfg.hostname}";
  issuer = "https://${cfg.identityHostname}/realms/terrace";
  runtime = "/run/terrace-vaultwarden-inputs";
  state = "/run/terrace-vaultwarden-state";
  ready = "${state}/ready";
  marker = "/var/lib/terrace-vaultwarden/migrated-release";
  source = "${cfg.inputsDirectory}/input.json";
  helpers = pkgs.runCommand "terrace-vaultwarden-helpers" {} ''
    mkdir $out
    cp ${./vaultwarden/runtime.ts} $out/runtime.ts
    cp ${./vaultwarden/policy.ts} $out/policy.ts
  '';
  expectedLedger = pkgs.runCommand "vaultwarden-expected-migrations-${release.pin.version}" {} ''
    for directory in ${release.server.src}/migrations/postgresql/*/; do
      basename "$directory" | cut -d_ -f1 | tr -d '-'
    done | LC_ALL=C sort >$out
    test "$(wc -l <$out)" -eq 48
  '';
  runtimeTool = command: "${pkgs.bun}/bin/bun --no-env-file ${helpers}/runtime.ts ${command} ${lib.escapeShellArgs [source origin issuer]}";
  ledgerCheck = pkgs.writeShellScript "terrace-vaultwarden-ledger-check" ''
    set -euo pipefail
    ${pkgs.util-linux}/bin/runuser -u postgres -- ${config.services.postgresql.package}/bin/psql -XAtw -v ON_ERROR_STOP=1 -d ${role} \
      -c 'SELECT version FROM __diesel_schema_migrations ORDER BY version' >${runtime}/ledger 2>/dev/null
    ${pkgs.diffutils}/bin/cmp -s ${expectedLedger} ${runtime}/ledger
  '';
  guard = pkgs.writeShellScript "terrace-vaultwarden-start-guard" ''
    set -euo pipefail
    rm -f ${ready}
    ${runtimeTool "check"}
    test ! -e ${state}/absent-saved-config.json
    test ! -L ${state}/absent-saved-config.json
    test "$(cat ${marker})" = ${release.server}
    ${ledgerCheck}
  '';
  bootstrapOwned = pkgs.writeShellScript "terrace-vaultwarden-bootstrap-owned" ''
    set -euo pipefail
    rm -f ${ready}
    ${guard}
    exec ${runtimeTool "bootstrap"} ${toString cfg.port}
  '';
  bootstrap = pkgs.writeShellScriptBin "terrace-vaultwarden-bootstrap" ''
    umask 077
    exec ${pkgs.util-linux}/bin/flock --exclusive --timeout 30 --conflict-exit-code 75 --no-fork \
      /run/terrace-vaultwarden-bootstrap.lock ${bootstrapOwned}
  '';
  inspect = pkgs.writeShellScriptBin "terrace-vaultwarden-inspect" ''
    # Read-only native admin diagnostics, using the protected local source.
    exec ${runtimeTool "inspect"} ${toString cfg.port}
  '';
  privateUnit = {
    UMask = "0077";
    LogNamespace = "terrace-vaultwarden";
    TimeoutStartSec = 180;
    TimeoutStopSec = 30;
    MemoryMax = "512M";
    TasksMax = 128;
    CPUQuota = "100%";
  };
  waitHTTP = port: ''
    ${pkgs.curl}/bin/curl --fail --silent --output /dev/null --connect-timeout 1 --max-time 3 \
      --retry 60 --retry-connrefused --retry-delay 1 --retry-max-time 90 http://127.0.0.1:${toString port}/alive 2>/dev/null
  '';
  dns = value:
    builtins.stringLength value
    <= 253
    && builtins.match "([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?" value != null
    && lib.all (part: builtins.stringLength part <= 63) (lib.splitString "." value)
    && builtins.match ".*[a-z].*" (lib.last (lib.splitString "." value)) != null
    && !(lib.hasSuffix ".localhost" value);
  pathOK = value: builtins.match "/[a-zA-Z0-9][a-zA-Z0-9/_.-]*" value != null && !(lib.hasPrefix "/nix/store" value) && !(lib.hasInfix "//" value) && !(lib.hasSuffix "/" value) && lib.all (part: part != "." && part != "..") (lib.splitString "/" value);
in {
  imports = [./backup.nix];
  options.terrace.vaultwarden = {
    enable = lib.mkEnableOption "optional native Vaultwarden with explicit migrations and approved-account bootstrap";
    hostname = lib.mkOption {type = lib.types.str;};
    identityHostname = lib.mkOption {type = lib.types.str;};
    inputsDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-vaultwarden-inputs";
    };
    certificateFile = lib.mkOption {type = lib.types.str;};
    certificateKeyFile = lib.mkOption {type = lib.types.str;};
    identityCaFile = lib.mkOption {
      type = lib.types.str;
      default = "/etc/ssl/certs/ca-certificates.crt";
      description = "Runtime readable CA bundle; TLS verification remains enabled.";
    };
    port = lib.mkOption {
      type = lib.types.port;
      default = 18222;
    };
    uid = lib.mkOption {
      type = lib.types.int;
      default = 44124;
    };
    manageDatabase = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Disable only for an already restored native database/role.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.versionAtLeast config.system.stateVersion "24.11" && pkgs.stdenv.hostPlatform.system == "x86_64-linux";
        message = "Vaultwarden requires the tested native x86-64 Linux state layout.";
      }
      {
        assertion = lib.all dns [cfg.hostname cfg.identityHostname] && cfg.hostname != cfg.identityHostname;
        message = "Vaultwarden requires distinct canonical application/identity hostnames.";
      }
      {
        assertion = !(config.terrace.identity.enable or false) || cfg.identityHostname == config.terrace.identity.hostname;
        message = "Vaultwarden identity must match the co-located native Terrace issuer.";
      }
      {
        assertion = lib.all pathOK [cfg.inputsDirectory cfg.certificateFile cfg.certificateKeyFile cfg.identityCaFile] && cfg.port >= 1024 && cfg.port <= 65533 && cfg.uid > 1000;
        message = "Vaultwarden requires external normalized runtime paths and unprivileged ports/UID.";
      }
    ];
    users.users.vaultwarden = {
      name = role;
      uid = cfg.uid;
    };
    users.groups.vaultwarden = {
      name = role;
      gid = cfg.uid;
    };
    services.postgresql = lib.mkIf cfg.manageDatabase {
      enable = true;
      ensureDatabases = [role];
      ensureUsers = [
        {
          name = role;
          ensureDBOwnership = true;
        }
      ];
    };
    systemd.tmpfiles.rules = ["d ${state} 0755 root root -" "d /var/lib/terrace-vaultwarden 0700 root root -"];
    services.vaultwarden = {
      enable = true;
      package = release.server;
      webVaultPackage = release.webvault;
      dbBackend = "postgresql";
      configureNginx = false;
      configurePostgres = false;
      environmentFile = ["${runtime}/runtime.env"];
      config = {
        ENV_FILE = "/dev/null";
        CONFIG_FILE = "${state}/absent-saved-config.json";
        DOMAIN = origin;
        DATABASE_URL = "postgresql://${role}@localhost/${role}?host=/run/postgresql";
        DATABASE_MAX_CONNS = 6;
        DATABASE_MIN_CONNS = 1;
        ROCKET_ADDRESS = "127.0.0.1";
        ROCKET_PORT = cfg.port;
        ROCKET_WORKERS = 2;
        LOG_LEVEL = "error";
        EXTENDED_LOGGING = false;
        SSO_ENABLED = true;
        SSO_ONLY = true;
        SSO_PKCE = true;
        SSO_SIGNUPS_ALLOWED = false;
        SSO_SIGNUPS_MATCH_EMAIL = true;
        SSO_ALLOW_UNKNOWN_EMAIL_VERIFICATION = false;
        SSO_SCOPES = "email profile";
        SSO_DEBUG_TOKENS = false;
        SIGNUPS_ALLOWED = false;
        SIGNUPS_DOMAINS_WHITELIST = "";
        INVITATIONS_ALLOWED = false;
        USE_SENDMAIL = false;
        PUSH_ENABLED = false;
        DISABLE_ICON_DOWNLOAD = true;
        DISABLE_ADMIN_TOKEN = false;
        SSL_CERT_FILE = cfg.identityCaFile;
        # Local dedicated IdP is intentional; UID-scoped egress below denies
        # non-loopback traffic even if an application request targets elsewhere.
        HTTP_REQUEST_BLOCK_NON_GLOBAL_IPS = false;
      };
    };
    systemd.services = {
      terrace-vaultwarden-inputs = {
        description = "Prepare protected Vaultwarden runtime snapshot with writers stopped";
        before = ["vaultwarden.service" "terrace-vaultwarden-migrate.service"];
        serviceConfig =
          privateUnit
          // {
            Type = "oneshot";
            RemainAfterExit = true;
            RuntimeDirectory = "terrace-vaultwarden-inputs";
            RuntimeDirectoryMode = "0700";
          };
        script = ''
          set -euo pipefail
          rm -f ${ready}
          test "$(systemctl show vaultwarden.service -p MainPID --value)" = 0
          test "$(systemctl show vaultwarden.service -p ControlPID --value)" = 0
          ${runtimeTool "prepare"}
        '';
      };
      terrace-vaultwarden-database = {
        requires = ["postgresql.target"];
        after = ["postgresql.target"];
        serviceConfig =
          privateUnit
          // {
            Type = "oneshot";
            RemainAfterExit = true;
          };
        script = ''
          ${pkgs.util-linux}/bin/runuser -u postgres -- ${config.services.postgresql.package}/bin/psql -Xw -v ON_ERROR_STOP=1 -d ${role} <<'SQL'
          ALTER ROLE terrace_vaultwarden NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 6;
          REVOKE CONNECT ON DATABASE terrace_vaultwarden FROM PUBLIC;
          GRANT CONNECT ON DATABASE terrace_vaultwarden TO terrace_vaultwarden;
          REVOKE ALL ON SCHEMA public FROM PUBLIC;
          SQL
        '';
      };
      terrace-vaultwarden-migration-server = {
        description = "Private systemd-owned native Vaultwarden migration process";
        requires = ["terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service"];
        after = ["vaultwarden.service" "terrace-vaultwarden-bootstrap.service" "terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service"];
        conflicts = ["vaultwarden.service" "terrace-vaultwarden-bootstrap.service"];
        partOf = ["terrace-vaultwarden-migrate.service"];
        unitConfig.StopWhenUnneeded = true;
        path = [pkgs.openssl];
        environment =
          lib.mapAttrs (_: value:
            if lib.isBool value
            then lib.boolToString value
            else toString value)
          config.services.vaultwarden.config
          // {
            DATA_FOLDER = "/var/lib/vaultwarden";
            WEB_VAULT_FOLDER = "${release.webvault}/share/vaultwarden/vault";
            ROCKET_PORT = toString (cfg.port + 2);
          };
        serviceConfig =
          (lib.removeAttrs config.systemd.services.vaultwarden.serviceConfig ["ExecStartPre" "ExecStartPost" "ExecStopPost" "ExecStart" "EnvironmentFile" "ReadWritePaths"])
          // {
            Type = "exec";
            ExecStart = "${release.server}/bin/vaultwarden";
            ExecStartPre = "+${pkgs.writeShellScript "terrace-vaultwarden-migration-close" ''
              rm -f ${ready} ${marker} ${runtime}/migration-stop.json
            ''}";
            EnvironmentFile = "${runtime}/runtime.env";
            ReadWritePaths = [state runtime "/var/lib/terrace-vaultwarden"];
            Restart = lib.mkForce "no";
            RuntimeMaxSec = 150;
            TimeoutStopSec = 15;
          };
      };
      terrace-vaultwarden-migrate = {
        description = "Explicit native Vaultwarden migrations on a private loopback port";
        conflicts = ["vaultwarden.service" "terrace-vaultwarden-bootstrap.service"];
        requires = ["terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service"];
        wants = ["terrace-vaultwarden-migration-server.service"];
        after = ["vaultwarden.service" "terrace-vaultwarden-bootstrap.service" "terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service" "terrace-vaultwarden-migration-server.service"];
        serviceConfig =
          privateUnit
          // {
            Type = "oneshot";
            TimeoutStopSec = 60;
            ExecStopPost = pkgs.writeShellScript "terrace-vaultwarden-migration-cleanup" ''
              set -euo pipefail
              if test "$SERVICE_RESULT" != success; then rm -f ${ready} ${marker}; fi
              if ! ${pkgs.python3}/bin/python3 ${./vaultwarden/migration-stop.py}; then
                rm -f ${ready} ${marker}
                exit 1
              fi
            '';
          };
        script = ''
          set -euo pipefail
          rm -f ${ready} ${marker}
          systemctl is-active --quiet terrace-vaultwarden-migration-server.service
          ${waitHTTP (cfg.port + 2)}
          ${ledgerCheck}
          test -s /var/lib/vaultwarden/rsa_key.pem
          ${pkgs.python3}/bin/python3 ${./vaultwarden/migration-stop.py} complete
          printf '%s\n' ${release.server} >${marker}
        '';
      };
      terrace-vaultwarden-guard = {
        description = "Validate private snapshot/release/complete native ledger before application start";
        requires = ["terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service"];
        after = ["terrace-vaultwarden-inputs.service" "terrace-vaultwarden-database.service"];
        before = ["vaultwarden.service"];
        serviceConfig =
          privateUnit
          // {
            Type = "oneshot";
            ExecStart = guard;
          };
      };
      vaultwarden = {
        requires = ["terrace-vaultwarden-guard.service"];
        after = ["terrace-vaultwarden-guard.service"];
        # The separate root bootstrap is ordered AFTER this unprivileged native
        # service. This dependency is deliberately Wants, with no reverse After.
        wants = ["terrace-vaultwarden-bootstrap.service"];
        serviceConfig =
          privateUnit
          // {
            ExecStartPre = ["+${pkgs.writeShellScript "terrace-vaultwarden-close-pre" "rm -f ${ready}"}"];
            ExecStartPost = pkgs.writeShellScript "terrace-vaultwarden-http-ready" (waitHTTP cfg.port);
            ExecStopPost = "+${pkgs.writeShellScript "terrace-vaultwarden-close" "rm -f ${ready}"}";
            ReadWritePaths = [state];
            Restart = lib.mkForce "on-failure";
            RestartSec = 10;
          };
        unitConfig = {
          StartLimitIntervalSec = 300;
          StartLimitBurst = 3;
        };
      };
      terrace-vaultwarden-bootstrap = {
        description = "Idempotent native approved-email reconciliation; no mail delivery";
        requires = ["vaultwarden.service"];
        after = ["vaultwarden.service"];
        serviceConfig =
          privateUnit
          // {
            Type = "oneshot";
            ExecStart = "${bootstrap}/bin/terrace-vaultwarden-bootstrap";
            ExecStopPost = pkgs.writeShellScript "terrace-vaultwarden-bootstrap-stop" ''
              if test "$SERVICE_RESULT" != success; then rm -f ${ready}; fi
            '';
          };
      };
      nginx.serviceConfig = {
        LoadCredential = ["terrace-vaultwarden-cert:${cfg.certificateFile}" "terrace-vaultwarden-key:${cfg.certificateKeyFile}"];
        LogNamespace = lib.mkDefault "terrace-vaultwarden";
        MemoryMax = lib.mkDefault "256M";
        TasksMax = lib.mkDefault 128;
      };
    };
    services.nginx = {
      enable = true;
      logError = lib.mkDefault "stderr warn";
      virtualHosts.${cfg.hostname} = {
        onlySSL = true;
        sslCertificate = "/run/credentials/nginx.service/terrace-vaultwarden-cert";
        sslCertificateKey = "/run/credentials/nginx.service/terrace-vaultwarden-key";
        extraConfig = ''
          access_log off;
          add_header Cache-Control "private, no-store" always;
          if ($ssl_server_name != ${cfg.hostname}) { return 421; }
          if ($host != ${cfg.hostname}) { return 421; }
        '';
        locations."^~ /admin".return = "403";
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString cfg.port}";
          recommendedProxySettings = false;
          proxyWebsockets = true;
          extraConfig = ''
            if (!-f ${ready}) { return 503; }
            proxy_set_header Host ${cfg.hostname};
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header Forwarded "";
            proxy_cache off;
            proxy_hide_header Cache-Control;
            proxy_hide_header CDN-Cache-Control;
            proxy_hide_header Surrogate-Control;
            add_header Cache-Control "private, no-store" always;
            proxy_connect_timeout 5s;
            proxy_read_timeout 30s;
            client_max_body_size 32m;
          '';
        };
      };
    };
    networking.hosts."127.0.0.1" = lib.optional (config.terrace.identity.enable or false) cfg.identityHostname;
    networking.firewall.allowedTCPPorts = [443];
    networking.firewall.extraCommands = ''
      iptables -w -A OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d 127.0.0.0/8 -j REJECT
      ip6tables -w -A OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d ::1/128 -j REJECT
    '';
    networking.firewall.extraStopCommands = ''
      iptables -w -D OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d 127.0.0.0/8 -j REJECT 2>/dev/null || true
      ip6tables -w -D OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d ::1/128 -j REJECT 2>/dev/null || true
    '';
    environment.etc."systemd/journald@terrace-vaultwarden.conf".text = ''
      [Journal]
      Storage=volatile
      RuntimeMaxUse=16M
      RuntimeMaxFileSize=4M
      MaxRetentionSec=1day
      ForwardToSyslog=no
      ForwardToConsole=no
      ForwardToWall=no
    '';
    terrace.backup = {
      postgresql.databases = [role];
      extraPaths.vaultwarden-inputs = cfg.inputsDirectory;
      extraPaths.vaultwarden-state = "/var/lib/vaultwarden";
      extraPaths.vaultwarden-release = "/var/lib/terrace-vaultwarden";
    };
    environment.systemPackages = [bootstrap inspect];
  };
}
