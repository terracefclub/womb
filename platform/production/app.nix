# Production application host: PostgreSQL 18, Keycloak, CMS and website behind
# the HTTPS gateway, with encrypted off-site backup. Every value that differs
# per deployment is a required option; every secret comes from sops-nix.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.production;
  app = cfg.app;
  secret = name: config.terrace.production.secrets.path.${name};
  # A missing secret leaves a marker (and fails the base assertion that names
  # it) instead of aborting evaluation without a message.
  placeholder = name: config.sops.placeholder.${name} or "<MISSING-SECRET:${name}>";
  issuer = "https://${app.identityHostname}/realms/terrace";
  realmTemplate = import ./realm-template.nix {
    inherit pkgs;
    contract = app.identityContract;
    webOrigin = "https://${app.webHostname}";
    cmsOrigin = "https://${app.cmsHostname}";
    placeholders = {
      webSecret = placeholder "identity/client/terrace-web";
      introspectionSecret = placeholder "identity/client/terrace-community";
      cmsSecret = placeholder "identity/client/terrace-cms";
      editorialSecret = placeholder "identity/client/terrace-editorial";
    };
  };
  migrated = "/var/lib/terrace-cms/migrated-revision";
  pin = builtins.fromJSON (builtins.readFile ../website-release.json);
in {
  imports = [
    ./base.nix
    ./secrets.nix
    ./tls.nix
    ../website-gateway.nix
    ../identity.nix
    ../backup.nix
    ./identity-admin.nix
  ];
  options.terrace.production.app = {
    webHostname = lib.mkOption {
      type = lib.types.str;
      description = "Required: public website hostname (origin https://<name>).";
    };
    cmsHostname = lib.mkOption {
      type = lib.types.str;
      description = "Required: public CMS hostname.";
    };
    identityHostname = lib.mkOption {
      type = lib.types.str;
      description = "Required: public Keycloak issuer hostname.";
    };
    identityContract = lib.mkOption {
      type = lib.types.package;
      description = "Required: the hash-pinned TFC realm producer (platform/identity-contract.nix).";
    };
    releaseMigrations = lib.mkOption {
      type = lib.types.nullOr (lib.types.listOf lib.types.str);
      default =
        if config.terrace.website.revision == pin.revision
        then pin.migrations
        else null;
      defaultText = lib.literalMD "the pinned migration list of `platform/website-release.json` when the host runs that revision, otherwise `null`";
      description = "The CMS migration names of the release this host runs, recorded in the system manifest. `terrace-production activate` and `rollback` compare the database's migration ledger with the target system's list; `null` (unknown) makes them refuse any release change.";
    };
    backup = {
      primary = {
        repository = lib.mkOption {
          type = lib.types.str;
          description = "Required: restic SFTP repository (Storage Box), `sftp:user@host:path`.";
        };
        knownHosts = lib.mkOption {
          type = lib.types.str;
          description = "Required: known_hosts line(s) pinning the SFTP server, verified out of band.";
        };
        port = lib.mkOption {
          type = lib.types.port;
          default = 22;
          description = "SSH port of the SFTP endpoint (Hetzner Storage Boxes use 23).";
        };
      };
      secondary = {
        enable = lib.mkEnableOption "the optional S3-compatible (B2) second backup target";
        repository = lib.mkOption {
          type = lib.types.str;
          description = "restic S3 repository, `s3:https://endpoint/bucket`.";
        };
      };
    };
  };
  config = {
    assertions = [
      {
        assertion = lib.hasPrefix "sftp:" app.backup.primary.repository && builtins.match "[^\n]+( [^\n]+)+(\n[^\n]+)*\n?" app.backup.primary.knownHosts != null;
        message = "Terrace production app backup needs an sftp: repository and a pinned known_hosts entry.";
      }
      {
        assertion = !app.backup.secondary.enable || lib.hasPrefix "s3:https://" app.backup.secondary.repository;
        message = "Terrace production secondary backup must be an s3:https:// repository.";
      }
      {
        assertion = builtins.elem config.services.postgresql.settings.listen_addresses ["127.0.0.1" "localhost"] && config.services.keycloak.settings.http-host == "127.0.0.1" && config.services.keycloak.settings.http-management-host == "127.0.0.1";
        message = "Terrace production keeps PostgreSQL and Keycloak (HTTP and management) on loopback.";
      }
      {
        assertion = config.services.keycloak.initialAdminPassword == null && (!(cfg.instance == "production") || !(config.systemd.services.keycloak.serviceConfig ? EnvironmentFile));
        message = "Terrace production refuses a Keycloak bootstrap administrator (initialAdminPassword or an EnvironmentFile); see the G-15 procedure.";
      }
      {
        assertion = lib.all (template: !(lib.hasInfix "CMS_LOCAL_BOOTSTRAP" config.sops.templates.${template}.content) && !(lib.hasInfix "CMS_BOOTSTRAP_" config.sops.templates.${template}.content)) ["terrace-website/cms.env" "terrace-website/web.env"];
        message = "Terrace production environment templates must not contain CMS bootstrap settings.";
      }
      {
        assertion = let
          consumers =
            [
              config.terrace.identity.databasePasswordFile
              config.terrace.identity.realmFile
              config.terrace.website.inputFiles.cms
              config.terrace.website.inputFiles.web
            ]
            # ACME certificates live in /var/lib/acme (tls.nix checks nginx reads exactly those).
            ++ lib.optionals (cfg.tls.mode == "manual") [
              config.terrace.identity.certificateFile
              config.terrace.identity.certificateKeyFile
              config.terrace.websiteGateway.certificateFile
              config.terrace.websiteGateway.certificateKeyFile
            ]
            ++ lib.concatMap (target: [target.passwordFile] ++ lib.optional (target.environmentFile != null) target.environmentFile ++ lib.optional (target.sftp.identityFile != null) target.sftp.identityFile)
            (lib.attrValues (lib.filterAttrs (_: target: target.enable) config.terrace.backup.targets));
        in
          config.terrace.website.inputFiles != null && lib.all (path: lib.hasPrefix "/run/secrets/" path) consumers;
        message = "Terrace production secret consumers must read sops-nix paths under /run/secrets, nothing else.";
      }
      {
        assertion = config.users.users.terrace_cms.uid == 44121 && config.users.users.terrace_web.uid == 44122 && config.users.groups.terrace_cms.gid == 44121 && config.users.groups.terrace_web.gid == 44122;
        message = "Terrace production keeps the static CMS and website account ids (44121, 44122) that restored files depend on.";
      }
    ];
    terrace.production = {
      enable = true;
      role = "app";
      secrets.features = lib.optional app.backup.secondary.enable "secondary-backup";
      publicHostnames = [app.webHostname app.cmsHostname app.identityHostname];
      publicTCPPorts = [22 443];
      publicUDPPorts = [];
      writers = ["postgresql" "keycloak" "nginx" "terrace-cms" "terrace-web"];
      writerHelpers = ["keycloakPostgreSQLInit" "terrace-website-inputs"];
      conditionalWriters = ["terrace-cms" "terrace-web"];
      stateWitness = "${config.services.postgresql.dataDir}/PG_VERSION";
      writerTimers = ["terrace-cms-sweep" "terrace-backup" "terrace-backup-check" "terrace-backup-freshness" "terrace-identity-health"];
      # Off after `adopt --restored` until `terrace-production arm-timers` (docs/restore-promotion.md, steps 15 and 21) ...
      restoreHeldTimers = ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness"];
      # ... and the sweep until `arm-timers --in-service`, at D5 after any erasure replay.
      restoreServiceHeldTimers = ["terrace-cms-sweep"];
      manifestExtra = {
        website = {
          inherit (config.terrace.website) revision;
          migrations = app.releaseMigrations;
        };
        versions = {
          keycloak = config.services.keycloak.package.version;
          postgresql = config.services.postgresql.package.version;
          headscale = null;
        };
      };
    };
    # PostgreSQL's own target would otherwise pull the server in at boot.
    systemd.targets.postgresql.wantedBy = lib.mkForce ["terrace-production.target"];

    sops.templates = {
      "terrace-website/cms.env" = {
        restartUnits = ["terrace-cms.service"];
        content = ''
          PAYLOAD_SECRET=${placeholder "website/payload-secret"}
          CMS_PUBLIC_DEPLOYMENT=1
          CMS_ORIGIN=https://${app.cmsHostname}
          CMS_AUTH_ISSUER=${issuer}
          CMS_AUTH_CLIENT_ID=terrace-cms
          CMS_AUTH_AUDIENCE=terrace-editorial
          CMS_AUTH_CLIENT_SECRET=${placeholder "identity/client/terrace-cms"}
          CMS_AUTH_INTROSPECTION_CLIENT_ID=terrace-editorial
          CMS_AUTH_INTROSPECTION_SECRET=${placeholder "identity/client/terrace-editorial"}
          MEMBER_AUTH_ISSUER=${issuer}
          MEMBER_AUTH_CLIENT_ID=terrace-web
          MEMBER_AUTH_AUDIENCE=terrace-community
          MEMBER_AUTH_INTROSPECTION_CLIENT_ID=terrace-community
          MEMBER_AUTH_INTROSPECTION_SECRET=${placeholder "identity/client/terrace-community"}
          LIFECYCLE_INTAKE_SECRET=${placeholder "website/lifecycle-intake"}
        '';
      };
      "terrace-website/web.env" = {
        restartUnits = ["terrace-web.service"];
        content = ''
          CMS_ORIGIN=https://${app.cmsHostname}
          CONTENT_SOURCE=cms
          ORIGIN=https://${app.webHostname}
          MEMBER_AUTH_ORIGIN=https://${app.webHostname}
          MEMBER_AUTH_ISSUER=${issuer}
          MEMBER_AUTH_CLIENT_ID=terrace-web
          MEMBER_AUTH_CLIENT_SECRET=${placeholder "identity/client/terrace-web"}
          LIFECYCLE_INTAKE_SECRET=${placeholder "website/lifecycle-intake"}
        '';
      };
      # Imported by Keycloak on its first start only; client-secret rotation is manual (G-15).
      "identity/terrace-realm.json".file = realmTemplate;
    };

    terrace.website = {
      enable = true;
      inputFiles = {
        cms = config.sops.templates."terrace-website/cms.env".path;
        web = config.sops.templates."terrace-website/web.env".path;
      };
    };
    terrace.websiteGateway = {
      enable = true;
      webHostname = app.webHostname;
      cmsHostname = app.cmsHostname;
      certificateFile = cfg.tls.certificateFile;
      certificateKeyFile = cfg.tls.keyFile;
    };
    terrace.identity = {
      enable = true;
      hostname = app.identityHostname;
      realmFile = config.sops.templates."identity/terrace-realm.json".path;
      databasePasswordFile = secret "identity/database-password";
      certificateFile = cfg.tls.certificateFile;
      certificateKeyFile = cfg.tls.keyFile;
    };
    # One explicit major version: restores need the same PostgreSQL major.
    services.postgresql.package = pkgs.postgresql_18;
    # psql for the on-host `terrace-secrets host-apply` and operator recovery steps.
    environment.systemPackages = [config.services.postgresql.package];
    # Server-to-server calls to the public origins stay on this host.
    networking.hosts."127.0.0.1" = cfg.publicHostnames;

    # Upstream keycloakPostgreSQLInit writes the role password through mktemp;
    # give it a private tmpfs so the value never reaches the root disk. The unit
    # runs as postgres, so the mount must be world-writable and sticky like /tmp.
    systemd.services.keycloakPostgreSQLInit = {
      serviceConfig.TemporaryFileSystem = "/tmp:mode=1777";
      # Its CREATE ROLE carries the cleartext password: if it ever failed, PostgreSQL would
      # log the statement text. This session never logs statements.
      environment.PGOPTIONS = "-c log_min_error_statement=panic -c log_statement=none -c log_min_duration_statement=-1";
      # Upstream runs `psql --file` without ON_ERROR_STOP, so a failed CREATE ROLE still
      # exits 0 and the unit reports success with no role. Make psql fail loudly.
      environment.PSQLRC = "${pkgs.writeText "terrace-keycloak-init-psqlrc" "\\set ON_ERROR_STOP on\n"}";
    };
    systemd.services.terrace-cms-migrate.serviceConfig.LimitCORE = 0;
    systemd.services.terrace-cms-sweep.serviceConfig.LimitCORE = 0;
    # Before the first explicit migration the CMS and website are skipped, not
    # failed; a revision mismatch still fails through the module's own gate
    # (requireMigration, on both units since the website was gated too).
    systemd.services.terrace-cms.unitConfig.ConditionPathExists = migrated;
    systemd.services.terrace-web.unitConfig.ConditionPathExists = migrated;

    environment.etc."terrace/backup/primary-known-hosts".text = app.backup.primary.knownHosts + "\n";
    terrace.backup = {
      enable = true;
      postgresql.databases = ["keycloak"];
      targets.primary = {
        enable = true;
        inherit (app.backup.primary) repository;
        passwordFile = secret "backup/primary/repository-password";
        sftp = {
          identityFile = secret "backup/primary/sftp-key";
          knownHostsFile = "/etc/terrace/backup/primary-known-hosts";
          inherit (app.backup.primary) port;
        };
      };
      targets.secondary = lib.mkIf app.backup.secondary.enable {
        enable = true;
        inherit (app.backup.secondary) repository;
        passwordFile = secret "backup/secondary/repository-password";
        environmentFile = secret "backup/secondary/environment";
      };
    };
  };
}
