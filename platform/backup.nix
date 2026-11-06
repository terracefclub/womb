{
  config,
  lib,
  pkgs,
  ...
}: let
  inherit (lib) mkOption mkEnableOption types mkIf;
  cfg = config.terrace.backup;
  stateDir = "/var/lib/terrace-backup";

  enabledTargets = lib.filterAttrs (_: t: t.enable) cfg.targets;
  names = lib.attrNames enabledTargets;

  # Every directory whose files are hashed at backup time and verified after restore.
  fileSets =
    lib.mapAttrs' (n: p:
      lib.nameValuePair "media-${n}" {
        root = p;
        excludes = [];
      })
    cfg.media
    // lib.mapAttrs' (n: p:
      lib.nameValuePair "config-${n}" {
        root = p;
        excludes = [];
      })
    cfg.extraPaths
    // lib.optionalAttrs cfg.headscale.enable {
      # The live SQLite files are captured as a consistent .backup snapshot instead.
      headscale = {
        root = cfg.headscale.stateDir;
        excludes = ["db.sqlite" "db.sqlite-wal" "db.sqlite-shm"];
      };
    };

  tvar = f: lib.mapAttrs (_: t: f t) enabledTargets;
  nullToEmpty = v:
    if v == null
    then ""
    else toString v;

  settings = ''
    ${lib.toShellVar "STATE" stateDir}
    ${lib.toShellVar "TARGETS" names}
    ${lib.toShellVar "T_REPO" (tvar (t: t.repository))}
    ${lib.toShellVar "T_PASSFILE" (tvar (t: t.passwordFile))}
    ${lib.toShellVar "T_ENVFILE" (tvar (t: nullToEmpty t.environmentFile))}
    ${lib.toShellVar "T_SFTP_KEY" (tvar (t: nullToEmpty t.sftp.identityFile))}
    ${lib.toShellVar "T_SFTP_KNOWN_HOSTS" (tvar (t: nullToEmpty t.sftp.knownHostsFile))}
    ${lib.toShellVar "T_SFTP_PORT" (tvar (t: toString t.sftp.port))}
    ${lib.toShellVar "DATABASES" cfg.postgresql.databases}
    ${lib.toShellVar "PG_USER" cfg.postgresql.user}
    ${lib.toShellVar "RESTORE_USER" "terrace-restore"}
    ${lib.toShellVar "BOOTSTRAP_HELPER" "${./backup/bootstrap-identity.py}"}
    ${lib.toShellVar "FILESET_ROOT" (lib.mapAttrs (_: v: v.root) fileSets)}
    ${lib.toShellVar "FILESET_EXCLUDES" (lib.mapAttrs (_: v: lib.concatStringsSep "|" v.excludes) fileSets)}
    ${lib.toShellVar "PROTECTED_PATHS" ([stateDir "/var/lib/postgresql" "/var/lib/headscale" cfg.headscale.stateDir] ++ lib.attrValues cfg.media ++ lib.attrValues cfg.extraPaths)}
    ${lib.toShellVar "HEADSCALE_ENABLE" (lib.optionalString cfg.headscale.enable "1")}
    ${lib.toShellVar "HEADSCALE_DB" "${cfg.headscale.stateDir}/db.sqlite"}
    ${lib.toShellVar "BACKUP_HOST" config.networking.hostName}
    ${lib.toShellVar "KEEP_LAST" (toString cfg.retention.keepLast)}
    ${lib.toShellVar "KEEP_DAILY" (toString cfg.retention.keepDaily)}
    ${lib.toShellVar "KEEP_WEEKLY" (toString cfg.retention.keepWeekly)}
    ${lib.toShellVar "KEEP_MONTHLY" (toString cfg.retention.keepMonthly)}
    ${lib.toShellVar "CHECK_SUBSET" cfg.check.readDataSubset}
    ${lib.toShellVar "MAX_AGE" (toString (cfg.staleAfterHours * 3600))}
    ${lib.toShellVar "UPLOAD_LIMIT_KIB" (nullToEmpty cfg.uploadLimitKiB)}
  '';

  common = builtins.readFile ./backup/common.sh;

  runtimeInputs = with pkgs; [
    coreutils
    diffutils
    findutils
    gawk
    gnugrep
    gnused
    jq
    openssh
    restic
    sqlite
    util-linux
    python3
    cfg.postgresql.package
  ];

  backupScript = pkgs.writeShellApplication {
    name = "terrace-backup";
    inherit runtimeInputs;
    excludeShellChecks = ["SC2034"]; # each script uses a subset of the generated settings
    text = settings + common + builtins.readFile ./backup/backup.sh;
  };
  restoreScript = pkgs.writeShellApplication {
    name = "terrace-backup-restore";
    inherit runtimeInputs;
    excludeShellChecks = ["SC2034"]; # each script uses a subset of the generated settings
    text = settings + common + builtins.readFile ./backup/restore.sh;
  };

  targetModule = {name, ...}: {
    options = {
      enable = mkEnableOption "the ${name} backup target";
      repository = mkOption {
        type = types.str;
        example = "sftp:u123456@u123456.your-storagebox.de:terrace-restic";
        description = ''
          restic repository. SFTP/Storage Box style (`sftp:user@host:path`) or
          S3-compatible (`s3:https://endpoint/bucket`, for example Backblaze B2).
        '';
      };
      passwordFile = mkOption {
        type = types.str;
        description = "Absolute path of the restic repository password file, outside the Nix store, root-owned, mode 0400.";
        example = "/run/secrets/terrace-backup/primary-password";
      };
      environmentFile = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          Optional root-owned 0400 file with `NAME=value` lines (for example
          `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for S3-compatible targets).
        '';
      };
      sftp = {
        identityFile = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "SSH private key (root-owned, mode 0400, outside the Nix store) for an `sftp:` repository.";
        };
        knownHostsFile = mkOption {
          type = types.nullOr types.str;
          default = null;
          description = "known_hosts file pinning the SFTP server's host key. Host key checking is always strict.";
        };
        port = mkOption {
          type = types.port;
          default = 22;
          description = "SSH port of the SFTP endpoint.";
        };
      };
    };
  };

  secretPaths = lib.concatMap (t: [t.passwordFile] ++ lib.optional (t.environmentFile != null) t.environmentFile ++ lib.optional (t.sftp.identityFile != null) t.sftp.identityFile) (lib.attrValues enabledTargets);
  validName = n: builtins.match "[a-zA-Z0-9_-]+" n != null;
  validDb = n: builtins.match "[a-zA-Z0-9_]+" n != null;

  resources = {
    MemoryMax = cfg.resources.memoryMax;
    MemoryHigh = cfg.resources.memoryHigh;
    CPUQuota = cfg.resources.cpuQuota;
    IOWeight = cfg.resources.ioWeight;
    TasksMax = 256;
    Nice = 10;
    IOSchedulingClass = "best-effort";
    IOSchedulingPriority = 7;
  };
  hardening = {
    StateDirectory = "terrace-backup";
    StateDirectoryMode = "0711";
    ProtectSystem = "strict";
    ReadWritePaths = ["-${cfg.headscale.stateDir}"];
    ProtectHome = true;
    PrivateTmp = true;
    NoNewPrivileges = true;
    ProtectKernelTunables = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
  };
  serviceEnv = {GOMEMLIMIT = cfg.resources.goMemoryLimit;};
in {
  options.terrace.backup = {
    enable = mkEnableOption "encrypted off-site restic backups with isolated restore tooling";

    targets = mkOption {
      type = types.attrsOf (types.submodule targetModule);
      default = {};
      example = lib.literalExpression ''
        {
          primary = {
            enable = true;
            repository = "sftp:u123456@u123456.your-storagebox.de:terrace-restic";
            passwordFile = "/run/secrets/terrace-backup/primary-password";
            sftp = {
              identityFile = "/run/secrets/terrace-backup/storagebox-key";
              knownHostsFile = "/etc/terrace/backup/storagebox-known-hosts";
            };
          };
          secondary = {
            enable = true;
            repository = "s3:https://s3.us-west-004.backblazeb2.com/terrace-restic";
            passwordFile = "/run/secrets/terrace-backup/secondary-password";
            environmentFile = "/run/secrets/terrace-backup/b2-env";
          };
        }
      '';
      description = ''
        Backup repositories, all disabled until enabled one by one. Conventionally
        `primary` is an SFTP/Storage Box repository and `secondary` an optional
        S3-compatible copy at a different provider. Each target gets its own
        encryption password and an independent success signal.
      '';
    };

    postgresql = {
      databases = mkOption {
        type = types.listOf types.str;
        default = [];
        example = ["cms" "community" "keycloak"];
        description = "Databases dumped with pg_dump (custom format) inside one REPEATABLE READ snapshot, plus roles via pg_dumpall --globals-only.";
      };
      user = mkOption {
        type = types.str;
        default = "postgres";
        description = "Operating-system user with superuser access over the local socket.";
      };
      package = mkOption {
        type = types.package;
        default =
          if config.services.postgresql.enable
          then config.services.postgresql.package
          else pkgs.postgresql;
        defaultText = "config.services.postgresql.package";
        description = "PostgreSQL client tools; use the same major version as the server.";
      };
    };

    media = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = {cms-uploads = "/var/lib/payload/media";};
      description = "Application media directories (name = absolute path). Backed up as files and hashed for reconciliation.";
    };

    extraPaths = mkOption {
      type = types.attrsOf types.str;
      default = {};
      example = {keycloak-realm = "/var/lib/keycloak/realm-export";};
      description = "Other required configuration or state directories (name = absolute path), hashed like media.";
    };

    headscale = {
      enable = mkOption {
        type = types.bool;
        default = lib.attrByPath ["terrace" "relay" "enable"] false config;
        defaultText = "config.terrace.relay.enable when the relay module is imported, otherwise false";
        description = "Back up Headscale: a consistent SQLite snapshot plus the Noise and DERP private keys.";
      };
      stateDir = mkOption {
        type = types.str;
        default = "/var/lib/headscale";
        description = "Headscale state directory holding db.sqlite and the private keys.";
      };
    };

    schedule = mkOption {
      type = types.str;
      default = "*-*-* 03:15:00";
      description = "systemd OnCalendar expression for the backup run.";
    };

    retention = {
      keepLast = mkOption {
        type = types.ints.unsigned;
        default = 3;
        description = "Always keep this many most recent snapshots.";
      };
      keepDaily = mkOption {
        type = types.ints.unsigned;
        default = 7;
      };
      keepWeekly = mkOption {
        type = types.ints.unsigned;
        default = 4;
      };
      keepMonthly = mkOption {
        type = types.ints.unsigned;
        default = 6;
      };
    };

    check = {
      schedule = mkOption {
        type = types.str;
        default = "Sun *-*-* 05:30:00";
        description = "systemd OnCalendar expression for `restic check`.";
      };
      readDataSubset = mkOption {
        type = types.str;
        default = "5%";
        description = "Portion of pack data re-read and verified by each check (restic --read-data-subset).";
      };
    };

    staleAfterHours = mkOption {
      type = types.ints.positive;
      default = 26;
      description = "A target is stale when its last successful run is older than this. Drives `terrace-backup age` and the freshness unit.";
    };

    uploadLimitKiB = mkOption {
      type = types.nullOr types.ints.positive;
      default = null;
      description = "Optional restic --limit-upload, in KiB/s.";
    };

    resources = {
      memoryMax = mkOption {
        type = types.str;
        default = "768M";
        description = "Hard systemd MemoryMax for backup and check runs.";
      };
      memoryHigh = mkOption {
        type = types.str;
        default = "640M";
        description = "Soft systemd MemoryHigh (throttling threshold).";
      };
      goMemoryLimit = mkOption {
        type = types.str;
        default = "512MiB";
        description = "GOMEMLIMIT for restic so the Go runtime collects before the cgroup limit.";
      };
      cpuQuota = mkOption {
        type = types.str;
        default = "50%";
        description = "systemd CPUQuota.";
      };
      ioWeight = mkOption {
        type = types.ints.between 1 10000;
        default = 20;
        description = "systemd IOWeight (default for normal services is 100).";
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = names != [];
        message = "terrace.backup.enable requires at least one enabled terrace.backup.targets.<name>; targets are off by default.";
      }
      {
        assertion = cfg.postgresql.databases != [] || cfg.media != {} || cfg.extraPaths != {} || cfg.headscale.enable;
        message = "terrace.backup has nothing to back up: set postgresql.databases, media, extraPaths or headscale.enable.";
      }
      {
        assertion = lib.all validDb cfg.postgresql.databases;
        message = "terrace.backup.postgresql.databases entries may only contain letters, digits and underscores.";
      }
      {
        assertion = lib.all validName (lib.attrNames cfg.media ++ lib.attrNames cfg.extraPaths ++ names);
        message = "terrace.backup media, extraPaths and target names may only contain letters, digits, '-' and '_'.";
      }
      {
        assertion = lib.all (p: lib.hasPrefix "/" p && !(lib.hasPrefix "/nix/store" p)) secretPaths;
        message = "terrace.backup secret files must be absolute paths outside the Nix store (use /run/secrets or similar).";
      }
      {
        assertion = lib.all (t: !(lib.hasPrefix "sftp:" t.repository) || (t.sftp.identityFile != null && t.sftp.knownHostsFile != null)) (lib.attrValues enabledTargets);
        message = "terrace.backup sftp: targets need sftp.identityFile and sftp.knownHostsFile (strict host key pinning).";
      }
      {
        assertion = !cfg.headscale.enable || config.services.headscale.enable;
        message = "terrace.backup.headscale.enable requires services.headscale.enable (see terrace.relay).";
      }
      {
        assertion = cfg.postgresql.databases == [] || config.services.postgresql.enable;
        message = "terrace.backup.postgresql.databases requires services.postgresql.enable on this host.";
      }
    ];

    users.users.terrace-restore = {
      isSystemUser = true;
      group = "terrace-restore";
      description = "Runs the throwaway PostgreSQL cluster used by terrace-backup-restore";
    };
    users.groups.terrace-restore = {};

    environment.systemPackages = [backupScript restoreScript];

    systemd.services.terrace-backup = {
      description = "Encrypted off-site backup (database dumps, media, Headscale state)";
      after = ["postgresql.service" "network-online.target"];
      wants = ["network-online.target"];
      environment = serviceEnv;
      serviceConfig =
        {
          Type = "oneshot";
          ExecStart = "${backupScript}/bin/terrace-backup run";
          TimeoutStartSec = "6h";
        }
        // resources
        // hardening;
    };
    systemd.timers.terrace-backup = {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = cfg.schedule;
        RandomizedDelaySec = "15m";
        Persistent = true;
      };
    };

    systemd.services.terrace-backup-check = {
      description = "Verify encrypted backup repositories (restic check)";
      after = ["network-online.target"];
      wants = ["network-online.target"];
      environment = serviceEnv;
      serviceConfig =
        {
          Type = "oneshot";
          ExecStart = "${backupScript}/bin/terrace-backup check";
          TimeoutStartSec = "6h";
        }
        // resources
        // hardening;
    };
    systemd.timers.terrace-backup-check = {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = cfg.check.schedule;
        RandomizedDelaySec = "30m";
        Persistent = true;
      };
    };

    # Fails (visible in `systemctl --failed`) while any target's last success is too old.
    systemd.services.terrace-backup-freshness = {
      description = "Fail when an off-site backup is older than ${toString cfg.staleAfterHours} hours";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${backupScript}/bin/terrace-backup age";
        StateDirectory = "terrace-backup";
        StateDirectoryMode = "0711";
      };
    };
    systemd.timers.terrace-backup-freshness = {
      wantedBy = ["timers.target"];
      timerConfig = {
        OnCalendar = "hourly";
        Persistent = true;
      };
    };
  };
}
