# sops-nix wiring shared by both production roles. Secret names, generators and
# restart units come from inventory.json, the single list the operator tool
# (terrace-secrets) also reads. Nix only ever sees names and paths.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.production;
  inventory = (builtins.fromJSON (builtins.readFile ./inventory.json)).secrets;
  wanted = entry:
    builtins.elem cfg.role entry.roles
    && entry.delivered
    && (entry.feature == "core" || builtins.elem entry.feature cfg.secrets.features);
  declared = builtins.filter wanted inventory;
  hostKey = "/etc/ssh/ssh_host_ed25519_key";
in {
  options.terrace.production.secrets = {
    file = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/terrace-secrets/secrets.sops.json";
      readOnly = true;
      description = "Host copy of the encrypted store (never the Nix store); written by `terrace-secrets install`.";
    };
    features = lib.mkOption {
      type = lib.types.listOf (lib.types.enum ["secondary-backup"]);
      default = [];
      description = "Optional inventory features whose secrets this host receives (set by the role modules).";
    };
    required = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      readOnly = true;
      description = "Inventory secrets this host must receive.";
    };
    path = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      readOnly = true;
      description = "Runtime path of each required secret, for consumers.";
    };
  };
  config = lib.mkIf cfg.enable {
    terrace.production.secrets.required = map (entry: entry.name) declared;
    terrace.production.secrets.path = lib.genAttrs (map (entry: entry.name) declared) (name: "/run/secrets/${name}");
    sops = {
      defaultSopsFile = cfg.secrets.file;
      defaultSopsFormat = "json";
      # The ciphertext lives outside the store, so it is neither copied to the
      # builder nor tied to a system generation: rotation needs no rebuild.
      validateSopsFiles = false;
      useSystemdActivation = true;
      useTmpfs = false;
      keepGenerations = 1;
      age = {
        sshKeyPaths = [hostKey];
        keyFile = null;
        generateKey = false;
      };
      gnupg.sshKeyPaths = [];
      secrets = lib.listToAttrs (map (entry:
        lib.nameValuePair entry.name {
          mode = "0400";
          restartUnits = entry.restartUnits;
        })
      declared);
    };
    # Only one host key exists, the operator-generated ed25519 key the installer
    # stages; it is also this host's age recipient.
    services.openssh.hostKeys = [
      {
        type = "ed25519";
        path = hostKey;
      }
    ];
    environment.etc."terrace/secrets-manifest.json".source = config.system.build.sops-nix-manifest;
    environment.etc."terrace/features".text = lib.concatStringsSep "," cfg.secrets.features + "\n";
    environment.systemPackages = [(pkgs.callPackage ./package.nix {})];
    systemd.tmpfiles.rules = ["d /var/lib/terrace-secrets 0700 root root - -"];
  };
}
