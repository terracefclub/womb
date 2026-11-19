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
        // The intake secret is shared by design: the website signs apply/redeem calls the CMS verifies.
        if (required.some(key => !env[key]) || Buffer.byteLength(env.LIFECYCLE_INTAKE_SECRET || "") < 32 ||
          Object.keys(env).some(key => key === "CMS_LOCAL_BOOTSTRAP" || key.startsWith("CMS_BOOTSTRAP_"))
        ${lib.optionalString (component == "web") ''|| env.CONTENT_SOURCE !== "cms" || Object.hasOwn(env,"DATABASE_URI") || Object.hasOwn(env,"PAYLOAD_SECRET") || Object.hasOwn(env,"MEMBER_AUTH_INTROSPECTION_SECRET") || Object.keys(env).some(key => key.startsWith("CMS_AUTH_"))''}
        ${lib.optionalString (component == "cms") ''|| Object.hasOwn(env,"MEMBER_AUTH_CLIENT_SECRET")''}) {
          console.error("Native Terrace runtime requires complete per-user identity and lifecycle intake configuration without bootstrap settings or cross-service secrets.");
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
        ReadWritePaths = [cfg.mediaDirectory cfg.outboxDirectory];
        MemoryHigh = "1536M";
        MemoryMax = "2G";
        CPUQuota = "200%";
      };
  };
  # Module-owned locations and origins override anything in the protected
  # environment file, so a runtime input cannot redirect the database, uploads
  # or the outbox that holds live invitation links.
  cmsEnvironment = ''
    export DATABASE_URI=${lib.escapeShellArg databaseURI}
    export CMS_MEDIA_DIR=${lib.escapeShellArg cfg.mediaDirectory}
    export LIFECYCLE_OUTBOX_DIR=${lib.escapeShellArg cfg.outboxDirectory}
    ${lib.optionalString (cfg.lifecyclePublicOrigin != null) "export LIFECYCLE_PUBLIC_ORIGIN=${lib.escapeShellArg cfg.lifecyclePublicOrigin}"}
  '';
  webEnvironment = ''
    ${lib.optionalString (cfg.cmsPublicOrigin != null) "export CMS_PUBLIC_ORIGIN=${lib.escapeShellArg cfg.cmsPublicOrigin}"}
    ${lib.optionalString (cfg.addressHeader != null) ''
      export ADDRESS_HEADER=${lib.escapeShellArg cfg.addressHeader}
      export XFF_DEPTH=${toString cfg.xffDepth}
      unset LIFECYCLE_DIRECT_CLIENTS
    ''}
  '';
  cmsExec = entry: ''
    ${cmsEnvironment}
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
    outboxDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-cms-outbox";
      description = "Private (0700, CMS account) LIFECYCLE_OUTBOX_DIR holding the member-lifecycle message mirror, including live invitation links. Backed up with the database.";
    };
    lifecyclePublicOrigin = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Bare public https website origin used in invitation links (LIFECYCLE_PUBLIC_ORIGIN). Required by a public CMS; the HTTPS gateway sets it from its website hostname.";
    };
    cmsPublicOrigin = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Public https CMS origin the website may embed media from (CMS_PUBLIC_ORIGIN). The HTTPS gateway sets it from its CMS hostname.";
    };
    addressHeader = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum ["X-Forwarded-For" "X-Real-IP"]);
      default = null;
      description = "Header a trusted proxy overwrites with the client address (website ADDRESS_HEADER). The HTTPS gateway sets X-Forwarded-For to exactly the connecting address.";
    };
    xffDepth = lib.mkOption {
      type = lib.types.ints.between 1 4;
      default = 1;
      description = "Number of trusted proxies writing X-Forwarded-For (website XFF_DEPTH).";
    };
    sweepSchedule = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 03:30:00";
      description = "systemd OnCalendar for the daily member-lifecycle sweep (CMS bin/sweep, without --purge-moderated).";
    };
    cmsUid = lib.mkOption {
      type = lib.types.int;
      default = 44121;
      description = "Static UID/GID of the CMS account. Restored uploads and the outbox keep numeric ownership, so a replacement host must assign the same number.";
    };
    webUid = lib.mkOption {
      type = lib.types.int;
      default = 44122;
      description = "Static UID/GID of the website account.";
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
        assertion = lib.all validDirectory [cfg.inputsDirectory cfg.mediaDirectory cfg.outboxDirectory cfg.databaseSocketDirectory];
        message = "Terrace website directories must be normalized absolute runtime paths outside the Nix store.";
      }
      {
        assertion = let
          disjoint = a: b: a != b && !(lib.hasPrefix (a + "/") b) && !(lib.hasPrefix (b + "/") a);
        in
          disjoint cfg.inputsDirectory cfg.mediaDirectory && disjoint cfg.inputsDirectory cfg.outboxDirectory && disjoint cfg.mediaDirectory cfg.outboxDirectory;
        message = "Terrace runtime inputs, uploads and the lifecycle outbox must not overlap.";
      }
      {
        assertion = cfg.cmsUid != cfg.webUid && cfg.cmsUid >= 10000 && cfg.cmsUid <= 60000 && cfg.webUid >= 10000 && cfg.webUid <= 60000;
        message = "Terrace website and CMS accounts need distinct static unprivileged UIDs.";
      }
      {
        assertion = lib.all (value: value == null || builtins.match "https://[a-z0-9.-]+" value != null) [cfg.lifecyclePublicOrigin cfg.cmsPublicOrigin];
        message = "Terrace public lifecycle and CMS media origins must be bare https origins.";
      }
    ];
    # Static IDs: a replacement host restores uploads and the outbox with their
    # numeric owner, so the accounts must not depend on allocation order.
    users.groups.${cmsUser}.gid = cfg.cmsUid;
    users.groups.${webUser}.gid = cfg.webUid;
    users.users.${cmsUser} = {
      isSystemUser = true;
      uid = cfg.cmsUid;
      group = cmsUser;
    };
    users.users.${webUser} = {
      isSystemUser = true;
      uid = cfg.webUid;
      group = webUser;
    };
    systemd.tmpfiles.rules = [
      "d ${cfg.mediaDirectory} 0700 ${cmsUser} ${cmsUser} - -"
      # The CMS forces this mode too; its mirror files are 0600 and the sweep runs as the same account.
      "d ${cfg.outboxDirectory} 0700 ${cmsUser} ${cmsUser} - -"
    ];
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
      before = ["terrace-cms.service" "terrace-web.service" "terrace-cms-migrate.service" "terrace-cms-sweep.service"];
      serviceConfig = {
        Type = "oneshot";
        ExecStart = inputCheck;
        LogNamespace = "terrace-apps";
      };
    };
    systemd.services.terrace-cms-migrate = lib.recursiveUpdate cmsRuntime {
      description = "Explicit CMS migration; never part of automatic startup";
      conflicts = ["terrace-cms.service" "terrace-web.service" "terrace-cms-sweep.service"];
      requires = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      after = ["terrace-website-inputs.service" "terrace-cms.service" "terrace-web.service" "terrace-cms-sweep.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      serviceConfig = {
        Type = "oneshot";
        TimeoutStartSec = 180;
      };
      script = ''
        set -euo pipefail
        ${cmsEnvironment}
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
      script = ''
        ${webEnvironment}
        exec ${cfg.webPackage}/bin/start
      '';
      postStart = health cfg.webPort "/events/";
    };
    # Daily member-lifecycle housekeeping: expire invitations, withdraw stale
    # applications, finish outbox mirror writes and redactions after erasure.
    # Same account, environment file, guards and migration gate as the CMS.
    systemd.services.terrace-cms-sweep = lib.recursiveUpdate cmsRuntime {
      description = "Terrace CMS member-lifecycle sweep (bin/sweep)";
      requires = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      after = ["terrace-website-inputs.service"] ++ lib.optional cfg.manageDatabase "postgresql.target";
      serviceConfig = {
        Type = "oneshot";
        ExecStartPre = [(identityGuard "cms") requireMigration];
        # bin/sweep bounds itself at 120 s (exit 2 when it times out).
        TimeoutStartSec = 150;
      };
      script = cmsExec "sweep";
    };
    systemd.timers.terrace-cms-sweep = {
      description = "Daily Terrace CMS member-lifecycle sweep";
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = cfg.sweepSchedule;
        Persistent = true;
        Unit = "terrace-cms-sweep.service";
      };
    };
    terrace.backup = lib.mkIf config.terrace.backup.enable {
      postgresql.databases = lib.mkIf cfg.manageDatabase ["terrace_cms"];
      media.website = cfg.mediaDirectory;
      extraPaths.website-inputs = cfg.inputsDirectory;
      # Live invitation links exist only here; restore them with the database.
      extraPaths.website-outbox = cfg.outboxDirectory;
    };
  };
}
