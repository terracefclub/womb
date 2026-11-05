{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.windmill;
  pin = builtins.fromJSON (builtins.readFile ./windmill/release.json);
  image = "${pin.imageName}:${pin.version}";
  imageFile = import ./windmill/image.nix {inherit pkgs;};
  membershipPython = import ./windmill-functional/membership-python.nix {inherit pkgs;};
  membershipWheels = (builtins.fromJSON (builtins.readFile ./windmill-functional/membership-wheels.json)).wheels;
  user = "terrace_windmill";
  server = "podman-terrace-windmill-server.service";
  worker = "podman-terrace-windmill-worker.service";
  origin = "https://${cfg.hostname}";
  databaseURL = "postgresql://${user}@localhost/${user}?host=/run/postgresql";
  ready = "/run/terrace-windmill-ready";
  marker = "/var/lib/terrace-windmill/migrated-image";
  backgroundSQL = lib.concatMapStringsSep "," (name: "'${name}'") pin.backgroundMigrations;
  validPath = value:
    builtins.match "/[a-zA-Z0-9][a-zA-Z0-9/_.-]*" value
    != null
    && !(lib.hasPrefix "/nix/store" value)
    && !(lib.hasInfix "//" value)
    && !(lib.hasSuffix "/" value)
    && lib.all (v: v != "." && v != "..") (lib.splitString "/" value);
  helpers = pkgs.runCommand "terrace-windmill-helpers" {} ''
    mkdir -p $out
    cp ${./windmill/bootstrap.ts} $out/bootstrap.ts
    cp ${./windmill/policy.ts} $out/policy.ts
    cp ${./windmill/provider.ts} $out/provider.ts
  '';
  bootstrapInvocation = "${pkgs.bun}/bin/bun --no-env-file ${helpers}/bootstrap.ts ${lib.escapeShellArg (cfg.inputsDirectory + "/bootstrap.json")} ${toString cfg.port} ${lib.escapeShellArg origin}";
  lockedBootstrap = pkgs.writeShellScript "terrace-windmill-bootstrap-owned" ''
    rm -f ${ready}
    echo 'Windmill launcher stage: ownership acquired' >&2
    exec ${bootstrapInvocation}
  '';
  bootstrapPackage = pkgs.writeShellScriptBin "terrace-windmill-bootstrap" ''
    # One root-private ownership lock spans input validation, reconciliation and
    # failure/readiness cleanup. --no-fork keeps it with the owning Bun process.
    umask 077
    echo 'Windmill launcher stage: waiting for ownership' >&2
    exec ${pkgs.util-linux}/bin/flock --exclusive --timeout 30 --conflict-exit-code 75 --no-fork \
      /run/terrace-windmill-bootstrap.lock ${lockedBootstrap}
  '';
  bootstrap = "${bootstrapPackage}/bin/terrace-windmill-bootstrap";
  recoveryPackage = pkgs.writeShellScriptBin "terrace-windmill-inspect" ''
    exec ${pkgs.coreutils}/bin/timeout --kill-after=5 45 ${pkgs.python3}/bin/python3 ${./windmill/recovery.py} ${lib.escapeShellArg cfg.inputsDirectory} ${toString cfg.port}
  '';
  commonEnvironment = {
    DATABASE_URL = databaseURL;
    SERVER_BIND_ADDR = "127.0.0.1";
    BASE_URL = origin;
    RUST_LOG = "warn";
    HOME = "/tmp";
    SSL_CERT_FILE = "/run/terrace-ca.crt";
    NUM_WORKERS = "1";
    KEEP_JOB_DIR = "false";
    FAVOR_UNSHARE_PID = "false";
    DISABLE_NSJAIL = "true";
  };
  commonOptions = ["--network=host" "--userns=host" "--read-only" "--cap-drop=ALL" "--security-opt=no-new-privileges" "--pids-limit=256" "--cpus=1" "--memory=1536m"];
  workerMounts = import ./windmill/worker-mounts.nix {inherit image;};
  temporaryOptions = mode:
    if mode == "worker"
    then workerMounts
    else ["--tmpfs=/tmp:rw,nodev,nosuid,mode=1777,size=512m,notmpcopyup"];
  container = mode: {
    inherit image imageFile;
    pull = "never";
    autoStart = false;
    user = "${toString cfg.uid}:${toString cfg.uid}";
    privileged = false;
    volumes = ["/run/postgresql:/run/postgresql:ro" "/etc/ssl/certs/ca-certificates.crt:/run/terrace-ca.crt:ro"];
    extraOptions =
      commonOptions
      ++ temporaryOptions mode
      ++ lib.optional (config.terrace.identity.enable or false) "--add-host=${config.terrace.identity.hostname}:127.0.0.1"
      ++ lib.optional (mode == "worker" && cfg.membershipRuntime) "--volume=${membershipPython}:/run/terrace-membership-python:ro";
    environment =
      commonEnvironment
      // {
        MODE = mode;
        SKIP_MIGRATION = "true";
        PORT = toString (
          if mode == "server"
          then cfg.port
          else cfg.port + 1
        );
        DATABASE_CONNECTIONS =
          if mode == "server"
          then "6"
          else "4";
      }
      // lib.optionalAttrs (mode == "worker" && cfg.membershipRuntime) {
        # Supported native worker settings: skip only these exact preprovided
        # lock rows and use this immutable SDK closure, without runtime PyPI.
        ADDITIONAL_PYTHON_PATHS = "/run/terrace-membership-python";
        PIP_LOCAL_DEPENDENCIES = lib.concatMapStringsSep "," (row: "^${row.name}==${lib.replaceStrings ["."] ["[.]"] row.version}$") membershipWheels;
      };
  };
  serviceLimits = {
    LogNamespace = "terrace-apps";
    UMask = "0077";
    MemoryMax = "2G";
    CPUQuota = "100%";
    TasksMax = 384;
    TimeoutStartSec = lib.mkForce 300;
    TimeoutStopSec = lib.mkForce 45;
    RestartSec = 10;
  };
  waitHTTP = port: ''
    ${pkgs.curl}/bin/curl --fail --silent --output /dev/null --connect-timeout 1 --max-time 3 \
      --retry 60 --retry-connrefused --retry-delay 1 --retry-max-time 120 \
      http://127.0.0.1:${toString port}/api/version 2>/dev/null
  '';
  migration = pkgs.writeShellScript "terrace-windmill-migrate" ''
    set -euo pipefail
    rm -f ${marker} ${ready}
    cleanup() { ${pkgs.podman}/bin/podman rm --force --ignore --cidfile "$RUNTIME_DIRECTORY/id" >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    echo 'Windmill migration stage: image load' >&2
    ${pkgs.podman}/bin/podman load --input ${imageFile} >/dev/null
    echo 'Windmill migration stage: original server start' >&2
    ${pkgs.podman}/bin/podman run --detach --rm --log-driver=journald --cidfile "$RUNTIME_DIRECTORY/id" \
      --name terrace-windmill-migration --pull=never \
      ${lib.escapeShellArgs (commonOptions ++ temporaryOptions "server")} --user=${toString cfg.uid}:${toString cfg.uid} \
      --volume /run/postgresql:/run/postgresql:ro \
      --env DATABASE_URL=${lib.escapeShellArg databaseURL} --env DATABASE_CONNECTIONS=6 \
      --env MODE=server --env PORT=${toString (cfg.port + 2)} --env SERVER_BIND_ADDR=127.0.0.1 \
      --env RUST_LOG=warn --env HOME=/tmp \
      ${lib.escapeShellArg image} >/dev/null
    echo 'Windmill migration stage: HTTP readiness' >&2
    ${waitHTTP (cfg.port + 2)}
    echo 'Windmill migration stage: complete schema ledger' >&2
    # Windmill starts three resumable background migrations after its SQLx ledger.
    # Do not mark this release migrated merely because its HTTP port opened.
    for attempt in $(seq 1 120); do
      complete=$(${pkgs.util-linux}/bin/runuser -u postgres -- ${config.services.postgresql.package}/bin/psql -XAt -d ${user} \
        -c "SELECT (SELECT bool_and(success) AND max(version)=${toString pin.latestMigration} FROM _sqlx_migrations) AND (SELECT count(*)=${toString (builtins.length pin.backgroundMigrations)} FROM windmill_migrations WHERE name IN (${backgroundSQL}))" 2>/dev/null || true)
      if test "$complete" = t; then
        printf '%s\n' ${lib.escapeShellArg pin.imageDigest} >${marker}
        exit 0
      fi
      sleep 1
    done
    echo 'Windmill migration ledger/background gate failed; runtime remains closed.' >&2
    exit 1
  '';
in {
  imports = [./backup.nix];
  options.terrace.windmill = {
    enable = lib.mkEnableOption "optional unchanged Windmill CE server/worker prototype";
    membershipRuntime = lib.mkEnableOption "pinned offline Python SDKs for the reviewed synthetic membership workload";
    hostname = lib.mkOption {type = lib.types.str;};
    port = lib.mkOption {
      type = lib.types.port;
      default = 18001;
    };
    uid = lib.mkOption {
      type = lib.types.int;
      default = 44123;
      description = "Dedicated host/container UID for peer authentication and scoped egress.";
    };
    inputsDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-windmill-inputs";
    };
    certificateFile = lib.mkOption {type = lib.types.str;};
    certificateKeyFile = lib.mkOption {type = lib.types.str;};
    manageDatabase = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Disable only for a restored isolated local PostgreSQL database/roles.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux" && cfg.port >= 1024 && cfg.port <= 65533 && cfg.uid >= 10000 && cfg.uid <= 60000;
        message = "Windmill requires Linux amd64 and dedicated unprivileged port/UID ranges.";
      }
      {
        assertion = builtins.match "([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z][a-z0-9-]*" cfg.hostname != null && !(lib.hasSuffix ".localhost" cfg.hostname);
        message = "Windmill requires a canonical DNS hostname.";
      }
      {
        assertion = lib.all validPath [cfg.inputsDirectory cfg.certificateFile cfg.certificateKeyFile];
        message = "Windmill inputs must use normalized external absolute runtime paths.";
      }
    ];
    environment.systemPackages = [bootstrapPackage recoveryPackage];
    users.groups.${user}.gid = cfg.uid;
    users.users.${user} = {
      isSystemUser = true;
      group = user;
      uid = cfg.uid;
    };
    services.postgresql = lib.mkIf cfg.manageDatabase {
      enable = true;
      ensureDatabases = [user];
      ensureUsers = [
        {
          name = user;
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
    systemd.services.terrace-windmill-db = lib.mkIf cfg.manageDatabase {
      requires = ["postgresql.target"];
      after = ["postgresql.target"];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "postgres";
        StandardOutput = "null";
        StandardError = "null";
        TimeoutStartSec = 30;
      };
      script = ''
        set -euo pipefail
        ${config.services.postgresql.package}/bin/psql -X -v ON_ERROR_STOP=1 -d ${user} <<'SQL'
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='windmill_user') THEN CREATE ROLE windmill_user NOLOGIN; END IF;
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='windmill_admin') THEN CREATE ROLE windmill_admin NOLOGIN BYPASSRLS; END IF;
          IF EXISTS (SELECT FROM pg_roles WHERE rolname IN ('windmill_user','windmill_admin') AND (rolcanlogin OR rolsuper OR rolcreaterole OR rolcreatedb)) OR
             NOT (SELECT rolbypassrls FROM pg_roles WHERE rolname='windmill_admin') OR
             (SELECT rolbypassrls FROM pg_roles WHERE rolname='windmill_user') THEN
            RAISE EXCEPTION 'Windmill existing group role policy rejected';
          END IF;
        END $$;
        ALTER ROLE terrace_windmill CONNECTION LIMIT 10;
        GRANT windmill_user TO windmill_admin;
        GRANT windmill_admin, windmill_user TO ${user};
        GRANT ALL PRIVILEGES ON DATABASE ${user} TO windmill_user;
        GRANT USAGE ON SCHEMA public TO windmill_admin, windmill_user;
        SQL
      '';
    };
    virtualisation.oci-containers = {
      backend = "podman";
      containers.terrace-windmill-server = container "server";
      containers.terrace-windmill-worker = container "worker";
    };
    systemd.services.terrace-windmill-migrate = {
      requires = ["postgresql.target"] ++ lib.optional cfg.manageDatabase "terrace-windmill-db.service";
      after = ["postgresql.target" server worker "terrace-windmill-bootstrap.service"] ++ lib.optional cfg.manageDatabase "terrace-windmill-db.service";
      conflicts = [server worker "terrace-windmill-bootstrap.service"];
      serviceConfig =
        serviceLimits
        // {
          Type = "oneshot";
          Environment = "PODMAN_SYSTEMD_UNIT=%n";
          ExecStart = migration;
          ExecStopPost = "${pkgs.podman}/bin/podman rm --force --ignore --cidfile=/run/terrace-windmill-migrate/id";
          StateDirectory = "terrace-windmill";
          StateDirectoryMode = "0700";
          RuntimeDirectory = "terrace-windmill-migrate";
          TimeoutStartSec = 300;
        };
    };
    systemd.services.podman-terrace-windmill-server = {
      requires = ["postgresql.target"] ++ lib.optional cfg.manageDatabase "terrace-windmill-db.service";
      after = ["postgresql.target"] ++ lib.optional cfg.manageDatabase "terrace-windmill-db.service";
      bindsTo = ["postgresql.service"];
      unitConfig = {
        StartLimitIntervalSec = 600;
        StartLimitBurst = 3;
      };
      serviceConfig = serviceLimits;
      preStart = lib.mkBefore ''
        rm -f ${ready}
        test -f ${marker} && test "$(cat ${marker})" = ${lib.escapeShellArg pin.imageDigest} || { echo 'Explicit Windmill migration is required.' >&2; exit 1; }
      '';
      postStart = ''
        echo 'Windmill runtime stage: HTTP readiness' >&2
        if ! (
          ${waitHTTP cfg.port}
        ); then
          status=$(${pkgs.curl}/bin/curl --silent --output /dev/null --write-out '%{http_code}' --connect-timeout 1 --max-time 3 http://127.0.0.1:${toString cfg.port}/api/version 2>/dev/null || true)
          case "$status" in [0-9][0-9][0-9]) echo "Windmill runtime HTTP status: $status" >&2;; esac
          exit 1
        fi
        echo 'Windmill runtime stage: protected bootstrap' >&2
        ${bootstrap}
      '';
      postStop = "rm -f ${ready}";
    };
    systemd.services.podman-terrace-windmill-worker = {
      requires = [server];
      after = [server];
      bindsTo = [server];
      unitConfig = {
        StartLimitIntervalSec = 600;
        StartLimitBurst = 3;
      };
      serviceConfig = serviceLimits;
    };
    systemd.services.terrace-windmill-bootstrap = {
      requires = [server];
      after = [server];
      bindsTo = [server];
      partOf = [server];
      serviceConfig =
        serviceLimits
        // {
          Type = "oneshot";
          ExecStart = bootstrap;
          TimeoutStartSec = 120;
          ExecStopPost = pkgs.writeShellScript "terrace-windmill-bootstrap-stop" ''
            if test "$SERVICE_RESULT" != success; then rm -f ${ready}; fi
          '';
        };
    };
    environment.etc."systemd/journald@terrace-apps.conf".text = ''
      [Journal]
      Storage=volatile
      RuntimeMaxUse=16M
      RuntimeMaxFileSize=4M
      MaxRetentionSec=1day
      ForwardToSyslog=no
      ForwardToConsole=no
      ForwardToWall=no
    '';
    # Native host service UID only; other host/cluster traffic is unchanged.
    networking.firewall.extraCommands = ''
      iptables -w -A OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d 127.0.0.0/8 -j REJECT
      ip6tables -w -A OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d ::1/128 -j REJECT
    '';
    networking.firewall.extraStopCommands = ''
      iptables -w -D OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d 127.0.0.0/8 -j REJECT 2>/dev/null || true
      ip6tables -w -D OUTPUT -m owner --uid-owner ${toString cfg.uid} ! -d ::1/128 -j REJECT 2>/dev/null || true
    '';
    services.nginx = {
      enable = true;
      virtualHosts.${cfg.hostname} = {
        onlySSL = true;
        sslCertificate = "/run/credentials/nginx.service/terrace-windmill-cert";
        sslCertificateKey = "/run/credentials/nginx.service/terrace-windmill-key";
        extraConfig = ''
          access_log off;
          add_header Cache-Control "private, no-store" always;
          if ($ssl_server_name != ${cfg.hostname}) { return 421; }
          if ($host != ${cfg.hostname}) { return 421; }
          if (!-f ${ready}) { return 503; }
        '';
        locations."~ ^/api/auth/login(/|$)".return = "403";
        locations."/" = {
          proxyPass = "http://127.0.0.1:${toString cfg.port}";
          recommendedProxySettings = false;
          extraConfig = ''
            proxy_set_header Host ${cfg.hostname};
            proxy_set_header X-Forwarded-Host ${cfg.hostname};
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Forwarded-Port 443;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_set_header Forwarded "";
            proxy_cache off;
            proxy_hide_header Cache-Control;
            proxy_hide_header Expires;
            proxy_hide_header CDN-Cache-Control;
            proxy_hide_header Cloudflare-CDN-Cache-Control;
            proxy_hide_header Surrogate-Control;
            add_header Cache-Control "private, no-store" always;
            proxy_connect_timeout 5s;
            proxy_read_timeout 30s;
          '';
        };
      };
    };
    systemd.services.nginx.serviceConfig = {
      LoadCredential = ["terrace-windmill-cert:${cfg.certificateFile}" "terrace-windmill-key:${cfg.certificateKeyFile}"];
      LogNamespace = "terrace-apps";
    };
    networking.firewall.allowedTCPPorts = [443];
    terrace.backup = lib.mkIf config.terrace.backup.enable {
      postgresql.databases = lib.mkIf cfg.manageDatabase [user];
      extraPaths.windmill-inputs = cfg.inputsDirectory;
      extraPaths.windmill-state = "/var/lib/terrace-windmill";
    };
  };
}
