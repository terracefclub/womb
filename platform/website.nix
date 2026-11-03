{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.website;
  pin = builtins.fromJSON (builtins.readFile ./website-release.json);
  cmsUser = "terrace_cms";
  webUser = "terrace_web";
  databaseURI = "postgresql://${cmsUser}@/terrace_cms?host=${cfg.databaseSocketDirectory}&port=5432";
  validDirectory = value:
    builtins.match "/[a-zA-Z0-9][a-zA-Z0-9/_.-]*" value
    != null
    && !(lib.hasSuffix "/" value)
    && !(lib.hasInfix "//" value)
    && !(lib.hasPrefix "/nix/store/" value)
    && value != "/nix/store"
    && lib.all (part: part != "." && part != "..") (lib.splitString "/" value);
  inputCheck = pkgs.writeShellScript "terrace-website-input-check" ''
    set -euo pipefail
    directory=${lib.escapeShellArg cfg.inputsDirectory}
    if ! test -d "$directory" || test -L "$directory" ||
       test "$(stat -c %u:%a "$directory")" != 0:700; then
      echo 'Terrace website input directory rejected; require a private root-owned runtime directory.' >&2
      exit 1
    fi
    for file in cms.env web.env; do
      path="$directory/$file"
      if ! test -f "$path" || test -L "$path" ||
         ! test "$(stat -c %u:%a "$path")" = 0:400; then
        echo 'Terrace website environment input rejected; require root-owned mode0400 regular files.' >&2
        exit 1
      fi
    done
    if ! ${pkgs.jq}/bin/jq -e '.component == "cms" and .revision == "${cfg.revision}" and .runtime.node == "${pin.node}"' ${cfg.cmsPackage}/release.json >/dev/null 2>&1 ||
       ! ${pkgs.jq}/bin/jq -e '.component == "web" and .revision == "${cfg.revision}" and .runtime.node == "${pin.node}"' ${cfg.webPackage}/release.json >/dev/null 2>&1; then
      echo 'Terrace website release manifest rejected; require one reviewed component pair.' >&2
      exit 1
    fi
  '';
  health = port: route: ''
    ${pkgs.curl}/bin/curl --fail --silent --output /dev/null --connect-timeout 2 --max-time 5 \
      --retry 30 --retry-connrefused --retry-delay 2 --retry-max-time 120 \
      http://127.0.0.1:${toString port}${route} 2>/dev/null
  '';
  identityGuard = component:
    pkgs.writeShellScript "terrace-${component}-identity-guard" ''
      exec ${cfg.nodePackage}/bin/node -e '
        const env = process.env;
        const required = ${builtins.toJSON (
        if component == "cms"
        then ["PAYLOAD_SECRET" "CMS_ORIGIN" "CMS_AUTH_ISSUER" "CMS_AUTH_CLIENT_ID" "CMS_AUTH_CLIENT_SECRET" "CMS_AUTH_AUDIENCE" "CMS_AUTH_INTROSPECTION_CLIENT_ID" "CMS_AUTH_INTROSPECTION_SECRET" "MEMBER_AUTH_ISSUER" "MEMBER_AUTH_CLIENT_ID" "MEMBER_AUTH_AUDIENCE" "MEMBER_AUTH_INTROSPECTION_CLIENT_ID" "MEMBER_AUTH_INTROSPECTION_SECRET"]
        else ["CMS_ORIGIN" "ORIGIN" "MEMBER_AUTH_ISSUER" "MEMBER_AUTH_CLIENT_ID" "MEMBER_AUTH_CLIENT_SECRET" "MEMBER_AUTH_ORIGIN"]
      )};
        if (required.some(key => !env[key]) || Object.keys(env).some(key => key === "CMS_LOCAL_BOOTSTRAP" || key.startsWith("CMS_BOOTSTRAP_"))
        ${lib.optionalString (component == "web") ''|| env.CONTENT_SOURCE !== "cms" || Object.hasOwn(env,"DATABASE_URI") || Object.hasOwn(env,"PAYLOAD_SECRET") || Object.hasOwn(env,"MEMBER_AUTH_INTROSPECTION_SECRET") || Object.keys(env).some(key => key.startsWith("CMS_AUTH_"))''}
        ${lib.optionalString (component == "cms") ''|| Object.hasOwn(env,"MEMBER_AUTH_CLIENT_SECRET")''}) {
          console.error("Native Terrace runtime requires complete per-user identity configuration without bootstrap settings or cross-service secrets.");
          process.exit(1);
        }
      '
    '';
  hardening = {
    UMask = "0077";
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectProc = "invisible";
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6"];
    TasksMax = 256;
    TimeoutStartSec = 150;
    TimeoutStopSec = 30;
    LogNamespace = "terrace-apps";
  };
  cmsRuntime = {
    path = [cfg.nodePackage pkgs.bash pkgs.coreutils];
    environment = {
      NODE_OPTIONS = "--max-old-space-size=1024";
      PORT = toString cfg.cmsPort;
    };
    serviceConfig =
      hardening
      // {
        User = cmsUser;
        Group = cmsUser;
        EnvironmentFile = "${cfg.inputsDirectory}/cms.env";
        ExecStartPre = identityGuard "cms";
        WorkingDirectory = cfg.cmsPackage;
        StateDirectory = "terrace-cms";
        StateDirectoryMode = "0700";
        CacheDirectory = "terrace-cms";
        CacheDirectoryMode = "0700";
        BindPaths = ["/var/cache/terrace-cms:${cfg.cmsPackage}/.next/cache"];
        ReadWritePaths = [cfg.mediaDirectory];
        MemoryHigh = "1536M";
        MemoryMax = "2G";
        CPUQuota = "200%";
      };
  };
  cmsExec = entry: ''
    export DATABASE_URI=${lib.escapeShellArg databaseURI}
    export CMS_MEDIA_DIR=${lib.escapeShellArg cfg.mediaDirectory}
    exec ${cfg.cmsPackage}/bin/${entry}
  '';
  requireMigration = pkgs.writeShellScript "terrace-require-cms-migration" ''
    set -euo pipefail
    if ! test -f /var/lib/terrace-cms/migrated-revision ||
       test "$(cat /var/lib/terrace-cms/migrated-revision)" != '${cfg.revision}'; then
      echo 'CMS migration gate is closed; run the explicit migration unit for this release.' >&2
      exit 1
    fi
  '';
in {
  imports = [./backup.nix];
  options.terrace.website = {
    enable = lib.mkEnableOption "optional native Terrace website and CMS (loopback only)";
    webPackage = lib.mkOption {
      type = lib.types.package;
      description = "Verified immutable website bundle with bin/start and release.json.";
    };
    cmsPackage = lib.mkOption {
      type = lib.types.package;
      description = "Verified immutable CMS bundle with bin/start, bin/migrate and empty .next/cache mount point.";
    };
    revision = lib.mkOption {
      type = lib.types.str;
      default = pin.revision;
      description = "Reviewed application revision required by the explicit migration gate.";
    };
    nodePackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.nodejs_24;
      description = "Exact Node runtime required by the accepted bundles.";
    };
    inputsDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-website-inputs";
      description = "Root-owned0700 runtime directory containing root-owned0400 cms.env and web.env. Backed up when terrace.backup is enabled.";
    };
    mediaDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-cms-media";
      description = "Retained CMS upload directory, included with the database in backups.";
    };
    webPort = lib.mkOption {
      type = lib.types.port;
      default = 14294;
    };
    cmsPort = lib.mkOption {
      type = lib.types.port;
      default = 13048;
    };
    manageDatabase = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Create the native CMS PostgreSQL database/peer role. Disable only for an already restored isolated local database.";
    };
    databaseSocketDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/run/postgresql";
      description = "Local PostgreSQL socket directory; no remote database or password is accepted by this module.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux" && lib.getVersion cfg.nodePackage == pin.node;
        message = "Terrace bundles require their pinned x86_64 Linux Node runtime.";
      }
      {
        assertion = builtins.match "[a-f0-9]{40}" cfg.revision != null && cfg.webPort != cfg.cmsPort && cfg.webPort >= 1024 && cfg.cmsPort >= 1024;
        message = "Terrace website requires one reviewed revision and distinct unprivileged loopback ports.";
      }
      {
        assertion = lib.all validDirectory [cfg.inputsDirectory cfg.mediaDirectory cfg.databaseSocketDirectory];
        message = "Terrace website directories must be normalized absolute runtime paths outside the Nix store.";
      }
      {
        assertion = cfg.inputsDirectory != cfg.mediaDirectory && !(lib.hasPrefix (cfg.inputsDirectory + "/") cfg.mediaDirectory) && !(lib.hasPrefix (cfg.mediaDirectory + "/") cfg.inputsDirectory);
        message = "Terrace runtime inputs and uploads must not overlap.";
      }
    ];
    users.groups.${cmsUser} = {};
    users.groups.${webUser} = {};
    users.users.${cmsUser} = {
      isSystemUser = true;
      group = cmsUser;
    };
    users.users.${webUser} = {
      isSystemUser = true;
      group = webUser;
    };
    systemd.tmpfiles.rules = ["d ${cfg.mediaDirectory} 0700 ${cmsUser} ${cmsUser} - -"];
    services.postgresql = lib.mkIf cfg.manageDatabase {
      enable = true;
      ensureDatabases = ["terrace_cms"];
      ensureUsers = [
        {
          name = cmsUser;
          ensureDBOwnership = true;
        }
      ];
      settings = {
        listen_addresses = lib.mkDefault "127.0.0.1";
        max_connections = lib.mkDefault 40;
        shared_buffers = lib.mkDefault "64MB";
      };
    };
    systemd.services.postgresql.serviceConfig = lib.mkIf cfg.manageDatabase {
      MemoryMax = lib.mkDefault "1G";
      CPUQuota = lib.mkDefault "200%";
    };

    # Private bounded volatile logs; never forward credential-bearing runtime errors.
    environment.etc."systemd/journald@terrace-apps.conf".text = ''
      [Journal]
      Storage=volatile
      RuntimeMaxUse=8M
      RuntimeMaxFileSize=2M
      MaxRetentionSec=1day
      ForwardToSyslog=no
      ForwardToConsole=no
      ForwardToWall=no
      RateLimitIntervalSec=30s
      RateLimitBurst=40
    '';
    systemd.services.terrace-website-inputs = {
      description = "Validate external Terrace website input permissions";
      path = [pkgs.coreutils];
      before = ["terrace-cms.service" "terrace-web.service" "terrace-cms-migrate.service"];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = inputCheck;
        LogNamespace = "terrace-apps";
      };
    };
    systemd.services.terrace-cms-migrate = lib.recursiveUpdate cmsRuntime {
      description = "Explicit CMS migration; never part of automatic startup";
      conflicts = ["terrace-cms.service" "terrace-web.service"];
      requires = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      after = ["terrace-website-inputs.service" "terrace-cms.service" "terrace-web.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      serviceConfig = {
        Type = "oneshot";
        TimeoutStartSec = 180;
      };
      script = ''
        set -euo pipefail
        export DATABASE_URI=${lib.escapeShellArg databaseURI}
        export CMS_MEDIA_DIR=${lib.escapeShellArg cfg.mediaDirectory}
        ${cfg.cmsPackage}/bin/migrate
        printf '%s\n' '${cfg.revision}' >/var/lib/terrace-cms/migrated-revision
      '';
    };
    systemd.services.terrace-cms = lib.recursiveUpdate cmsRuntime {
      description = "Terrace CMS immutable production runtime";
      wantedBy = ["multi-user.target"];
      requires = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      after = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      unitConfig = {
        StartLimitIntervalSec = 600;
        StartLimitBurst = 3;
      };
      serviceConfig = {
        ExecStartPre = [(identityGuard "cms") requireMigration];
        # Next returns143 after handling SIGTERM during a controlled stop.
        SuccessExitStatus = [143];
        Restart = "on-failure";
        RestartSec = 10;
      };
      script = cmsExec "start";
      postStart = health cfg.cmsPort "/api/events?limit=1";
    };
    systemd.services.terrace-web = {
      description = "Terrace website immutable production runtime";
      wantedBy = ["multi-user.target"];
      wants = ["terrace-cms.service"];
      requires = ["terrace-website-inputs.service"];
      after = ["terrace-website-inputs.service" "terrace-cms.service"];
      path = [cfg.nodePackage pkgs.bash pkgs.coreutils];
      environment = {
        NODE_OPTIONS = "--max-old-space-size=384";
        PORT = toString cfg.webPort;
      };
      unitConfig = {
        StartLimitIntervalSec = 600;
        StartLimitBurst = 3;
      };
      serviceConfig =
        hardening
        // {
          User = webUser;
          Group = webUser;
          EnvironmentFile = "${cfg.inputsDirectory}/web.env";
          ExecStartPre = identityGuard "web";
          WorkingDirectory = cfg.webPackage;
          MemoryHigh = "512M";
          MemoryMax = "768M";
          CPUQuota = "100%";
          Restart = "on-failure";
          RestartSec = 10;
        };
      script = "exec ${cfg.webPackage}/bin/start";
      postStart = health cfg.webPort "/events/";
    };
    terrace.backup = lib.mkIf config.terrace.backup.enable {
      postgresql.databases = lib.mkIf cfg.manageDatabase ["terrace_cms"];
      media.website = cfg.mediaDirectory;
      extraPaths.website-inputs = cfg.inputsDirectory;
    };
  };
}
