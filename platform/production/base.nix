# Shared production host policy for the app and relay roles: required
# parameters, operator SSH, the adoption boot gate and the invariants that make
# evaluation fail instead of deploying something unsafe. See
# docs/production-profile.md. The role modules import this file; the sops-nix
# module itself comes from the platform flake (nixosModules.production-*).
{
  config,
  lib,
  pkgs,
  options,
  ...
}: let
  cfg = config.terrace.production;
  tool = pkgs.callPackage ./package.nix {};
  marker = "/var/lib/terrace-production/adopted";
  # The documented restore destination (terrace-backup-restore --dest) and its reconciliation report.
  restoreMarker = "/var/lib/terrace-restore/reconciliation.txt";
  # Core dumps would put process memory (with secrets) on disk: pipe them to a no-op.
  noCore = "|${pkgs.coreutils}/bin/false";
  portDefinitions = option:
    lib.concatMap (definition:
      map (port: {
        inherit port;
        inherit (definition) file;
      })
      definition.value)
    option.definitionsWithLocations;
  # Every firewall port must come from a module that is allowed to open it: a port added
  # anywhere else is refused here, not silently overridden.
  portSources = kind: allowed: lib.filter (entry: !(builtins.elem entry.port allowed)) (portDefinitions options.networking.firewall.${kind});
  # Free-form firewall, SSH and sudo configuration may come only from NixOS's own modules.
  foreignDefinitions = option: lib.filter (definition: !(lib.hasInfix "/nixos/modules/" (toString definition.file))) option.definitionsWithLocations;
  foreignConfig = lib.concatMap foreignDefinitions [
    options.networking.firewall.extraCommands
    options.networking.firewall.extraStopCommands
    options.networking.firewall.extraInputRules
    options.networking.nftables.tables
    options.networking.nftables.ruleset
    options.services.openssh.extraConfig
    options.security.sudo.extraConfig
  ];
  strayTCP = portSources "allowedTCPPorts" cfg.publicTCPPorts;
  strayUDP = portSources "allowedUDPPorts" cfg.publicUDPPorts;
  production = cfg.instance == "production";
  reservedSuffixes = ["invalid" "test" "example" "localhost" "local" "localdomain" "internal" "intranet" "private" "lan" "home" "corp" "home.arpa" "example.com" "example.net" "example.org"];
  reserved = name: lib.any (suffix: name == suffix || lib.hasSuffix ".${suffix}" name) reservedSuffixes;
  dnsName = value:
    builtins.stringLength value
    <= 253
    && builtins.match "([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]([a-z0-9-]*[a-z0-9])?" value != null;
  # Test inputs and fixtures known in this repository; none may reach production.
  fixtureUnits = ["gateway-test-inputs" "cloud-rehearsal-inputs" "cloud-rehearsal-tls" "cloud-rehearsal-relay-address" "website-test-inputs" "documenso-test-mailpit" "rehearsal-pg-sample" "terrace-rehearsal-stage"];
  mib = value: let
    parts = builtins.match "([0-9]+)([KMGT]?)" (toString value);
    number = lib.toInt (builtins.elemAt parts 0);
    unit = builtins.elemAt parts 1;
  in
    if parts == null
    then throw "Terrace production: MemoryMax ${toString value} is not a plain size."
    else if unit == "T"
    then number * 1048576
    else if unit == "G"
    then number * 1024
    else if unit == "M"
    then number
    else if unit == "K"
    then number / 1024
    else number / 1048576;
  memoryUnits = cfg.writers ++ ["terrace-backup"];
  memoryOf = unit: config.systemd.services.${unit}.serviceConfig.MemoryMax or null;
  committedMiB = lib.foldl' (sum: unit: sum + mib (memoryOf unit)) 0 (lib.filter (unit: memoryOf unit != null) memoryUnits);
  secretLike = name: builtins.match ".*(SECRET|PASSWORD|PASSWD|TOKEN|PRIVATE_KEY|API_KEY|CREDENTIAL).*" name != null;
  # Both `environment` and raw `serviceConfig.Environment` assignments ("NAME=value").
  rawEnvironment = service: let
    value = service.serviceConfig.Environment or [];
  in
    lib.concatMap (item: lib.filter (part: part != "") (lib.splitString " " (toString item))) (lib.toList value);
  serviceEnvironmentLeaks = lib.concatLists (lib.mapAttrsToList (unit: service:
    lib.mapAttrsToList (name: _: "${unit}:${name}") (lib.filterAttrs (name: value:
      value
      != null
      && (secretLike name || lib.hasInfix "<SOPS:" (toString value)))
    service.environment)
    ++ map (assignment: "${unit}:${assignment}") (lib.filter (assignment: secretLike (builtins.head (lib.splitString "=" assignment)) || lib.hasInfix "<SOPS:" assignment) (rawEnvironment service)))
  config.systemd.services);
  sopsItems = lib.attrValues config.sops.secrets ++ lib.attrValues config.sops.templates;
  rootOnly = item: item.mode == "0400" && item.owner == null && (item.uid == 0 || item.uid == null) && item.group == null && (item.gid == 0 || item.gid == null);
  optionalModuleOn = path: lib.attrByPath (path ++ ["enable"]) false config;
  adopt = pkgs.writeShellApplication {
    name = "terrace-production";
    runtimeInputs = [pkgs.coreutils pkgs.systemd tool];
    text = ''
      [[ $(id -u) == 0 ]] || { echo 'terrace-production: run as root' >&2; exit 1; }
      restored=${restoreMarker}
      populated=${lib.escapeShellArg cfg.stateWitness}
      case "''${1:-}" in
        adopt)
          case "''${2:-}" in
            --new)
              # A new deployment starts from nothing: refuse a host that already holds state or a restore.
              if [[ -e $populated || -e $restored ]]; then
                echo 'terrace-production: this host holds data or a restore; use adopt --restored after a reconciled restore' >&2
                exit 1
              fi ;;
            --restored)
              if ! grep -qx 'RECONCILIATION OK' "$restored" 2>/dev/null || [[ ! -e $populated ]]; then
                echo 'terrace-production: no reconciled restore promoted into place; restore first (docs/production-profile.md)' >&2
                exit 1
              fi ;;
            *) echo 'usage: terrace-production adopt --new|--restored' >&2; exit 2 ;;
          esac
          # Fail closed: the writers start only on a host whose secrets verify.
          terrace-secrets verify
          systemctl start terrace-production.target
          # A writer whose dependency failed stays inactive, so check this profile's own units.
          failed=()
          for unit in ${lib.escapeShellArgs (map (unit: "${unit}.service") (cfg.writers ++ cfg.writerHelpers))}; do
            if systemctl is-failed --quiet "$unit"; then failed+=("$unit"); fi
          done
          if (( ''${#failed[@]} )); then
            echo "terrace-production: not adopted; failed: ''${failed[*]}" >&2
            exit 1
          fi
          install -d -m 0700 /var/lib/terrace-production
          if [[ ! -e ${marker} ]]; then
            install -m 0600 /dev/null ${marker}
            date -u +%FT%TZ >${marker}
          fi
          echo 'Adopted: the writers start now and at every boot.'
          ;;
        status)
          if [[ -e ${marker} ]]; then echo "adopted $(cat ${marker})"; else echo 'not adopted: no writer starts at boot'; fi
          systemctl is-active terrace-production.target || true
          ;;
        *) echo 'usage: terrace-production adopt --new|--restored | status' >&2; exit 2 ;;
      esac
    '';
  };
in {
  options.terrace.production = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      internal = true;
      description = "Set by a role module (app.nix or relay.nix).";
    };
    role = lib.mkOption {
      type = lib.types.enum ["app" "relay"];
      internal = true;
      description = "The single role of this host; importing both roles is a conflict.";
    };
    instance = lib.mkOption {
      type = lib.types.enum ["production" "rehearsal"];
      description = "Required: `production` for a real host; `rehearsal` only for the synthetic VM proof of this same profile.";
    };
    operatorKeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      description = "Required: individual operator SSH public keys (ed25519).";
    };
    memoryMiB = lib.mkOption {
      type = lib.types.ints.positive;
      description = "Required: host memory in MiB. The writers' MemoryMax ceilings must fit inside it.";
    };
    rehearsalHarness = lib.mkOption {
      type = lib.types.bool;
      default = false;
      internal = true;
      description = "Set only by the rehearsal harness (rehearsal.nix); refused in production.";
    };
    publicHostnames = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      internal = true;
    };
    publicTCPPorts = lib.mkOption {
      type = lib.types.listOf lib.types.port;
      internal = true;
    };
    publicUDPPorts = lib.mkOption {
      type = lib.types.listOf lib.types.port;
      internal = true;
    };
    escrowRecipient = lib.mkOption {
      type = lib.types.str;
      description = "Required: the offline escrow key's PUBLIC age recipient (not secret). The host accepts only ciphertext encrypted to exactly itself and this recipient.";
    };
    stateWitness = lib.mkOption {
      type = lib.types.str;
      internal = true;
      description = "A path that exists once the host holds application state (adopt --new refuses it).";
    };
    writerHelpers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      internal = true;
      default = [];
      description = "Services the writers depend on; their failure blocks adoption.";
    };
    writers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      internal = true;
      description = "Services started only by terrace-production.target.";
    };
    writerTimers = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      internal = true;
      description = "Timers armed only by terrace-production.target.";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = cfg.operatorKeys != [] && lib.all (key: builtins.match "ssh-ed25519 [A-Za-z0-9+/]{68}( [^\n]*)?" key != null) cfg.operatorKeys;
          message = "Terrace production requires at least one individual ed25519 operator SSH public key.";
        }
        {
          assertion = lib.all dnsName cfg.publicHostnames && lib.unique cfg.publicHostnames == cfg.publicHostnames;
          message = "Terrace production hostnames must be distinct lowercase DNS names.";
        }
        {
          assertion = !production || !(lib.any reserved cfg.publicHostnames);
          message = "Terrace production refuses a synthetic or reserved hostname (${toString (lib.filter reserved cfg.publicHostnames)}); use `instance = \"rehearsal\"` only for the VM proof.";
        }
        {
          assertion = !production || !cfg.rehearsalHarness;
          message = "Terrace production refuses the rehearsal harness (platform/production/rehearsal.nix).";
        }
        {
          assertion = !production || lib.intersectLists fixtureUnits (lib.attrNames config.systemd.services) == [];
          message = "Terrace production refuses test fixture units: ${toString (lib.intersectLists fixtureUnits (lib.attrNames config.systemd.services))}.";
        }
        {
          assertion = !production || config.security.pki.certificateFiles == [];
          message = "Terrace production refuses extra trust anchors (security.pki.certificateFiles is for synthetic CAs).";
        }
        {
          assertion = !production || (config.fileSystems ? "/" && (config.boot.loader.systemd-boot.enable || (config.boot.loader.grub.enable && config.boot.loader.grub.device != "nodev")));
          message = "Terrace production needs its per-host hardware module: a root filesystem and an installable boot loader.";
        }
        {
          assertion = lib.all (swap: swap.randomEncryption.enable) config.swapDevices;
          message = "Terrace production refuses unencrypted swap: decrypted secrets and runtime files must never reach disk.";
        }
        {
          # Runbook 6.22: at least 25 percent of memory stays free, so the ceilings fit in 75 percent.
          assertion = committedMiB * 4 <= cfg.memoryMiB * 3 && lib.all (unit: memoryOf unit != null) memoryUnits;
          message = "Terrace production writers commit ${toString committedMiB} MiB of MemoryMax (every writer needs one); with 25 percent headroom the host needs ${toString ((committedMiB * 4 + 2) / 3)} MiB and declares ${toString cfg.memoryMiB} MiB.";
        }
        {
          assertion = !config.services.k3s.enable && !(optionalModuleOn ["terrace" "documenso"]) && !(optionalModuleOn ["terrace" "windmill"]) && !(optionalModuleOn ["terrace" "vaultwarden"]) && !(optionalModuleOn ["terrace" "demos"]);
          message = "Terrace production does not yet enable K3s, Documenso, Windmill, Vaultwarden or the demos (docs/production-profile.md).";
        }
        {
          assertion = lib.all rootOnly sopsItems;
          message = "Terrace production secrets and templates must be root:root 0400.";
        }
        {
          assertion = !(lib.hasPrefix builtins.storeDir (toString config.sops.defaultSopsFile)) && !config.sops.validateSopsFiles && config.sops.useSystemdActivation && !config.sops.useTmpfs && config.sops.age.sshKeyPaths == ["/etc/ssh/ssh_host_ed25519_key"] && config.sops.age.keyFile == null && config.sops.gnupg.sshKeyPaths == [];
          message = "Terrace production secrets must come from the host's ed25519 key into ramfs, from ciphertext outside the Nix store.";
        }
        {
          assertion = lib.all (name: config.sops.secrets ? ${name}) config.terrace.production.secrets.required;
          message = "Terrace production secret(s) not declared: ${toString (lib.filter (name: !(config.sops.secrets ? ${name})) config.terrace.production.secrets.required)}.";
        }
        {
          assertion = builtins.match "age1[02-9ac-hj-np-z]{58}" cfg.escrowRecipient != null;
          message = "Terrace production needs the escrow key's public age recipient (terrace.production.escrowRecipient).";
        }
        {
          assertion = lib.all (secret: secret.sopsFile == config.sops.defaultSopsFile) (lib.attrValues config.sops.secrets);
          message = "Terrace production secrets come only from the host ciphertext; no per-secret sopsFile.";
        }
        {
          assertion = !config.systemd.coredump.enable && config.boot.kernel.sysctl."kernel.core_pattern" == noCore && config.boot.kernel.sysctl."fs.suid_dumpable" == 0 && lib.all (unit: (config.systemd.services.${unit}.serviceConfig.LimitCORE or null) == 0) cfg.writers;
          message = "Terrace production writes no core dumps: process memory holds secrets.";
        }
        {
          assertion = strayTCP == [] && strayUDP == [] && foreignConfig == [];
          message = "Terrace production refuses firewall ports or rules outside the role's public ports, and free-form firewall, sshd or sudo configuration: ${toString (map (entry: "${toString entry.port} from ${toString entry.file}") (strayTCP ++ strayUDP) ++ map (definition: toString definition.file) foreignConfig)}.";
        }
        {
          assertion = !config.users.mutableUsers && !config.systemd.enableEmergencyMode && config.services.openssh.settings.AllowUsers == ["operator"];
          message = "Terrace production needs immutable users, no emergency shell and SSH for the operator account only.";
        }
        {
          assertion = serviceEnvironmentLeaks == [];
          message = "Terrace production units must not carry secrets in Environment=: ${toString serviceEnvironmentLeaks}.";
        }
        {
          assertion = config.networking.firewall.enable && lib.sort lib.lessThan (lib.unique config.networking.firewall.allowedTCPPorts) == lib.sort lib.lessThan cfg.publicTCPPorts && lib.unique config.networking.firewall.allowedUDPPorts == cfg.publicUDPPorts && config.networking.firewall.allowedTCPPortRanges == [] && config.networking.firewall.allowedUDPPortRanges == [] && config.networking.firewall.interfaces == {} && lib.subtractLists ["lo"] config.networking.firewall.trustedInterfaces == [];
          message = "Terrace production ${cfg.role} firewall must open exactly TCP ${toString cfg.publicTCPPorts} and UDP ${toString cfg.publicUDPPorts}.";
        }
      ];

      system.stateVersion = lib.mkDefault "26.05";
      users.mutableUsers = false;
      systemd.enableEmergencyMode = false;
      systemd.coredump.enable = false;
      boot.kernel.sysctl = {
        "kernel.core_pattern" = lib.mkForce noCore;
        "fs.suid_dumpable" = 0;
      };
      environment.etc = {
        "terrace/role".text = cfg.role + "\n";
        "terrace/instance".text = cfg.instance + "\n";
        "terrace/escrow-recipient".text = cfg.escrowRecipient + "\n";
      };
      services.openssh = {
        enable = true;
        ports = [22];
        settings = {
          PermitRootLogin = "no";
          PasswordAuthentication = false;
          KbdInteractiveAuthentication = false;
          PermitEmptyPasswords = false;
          AllowUsers = ["operator"];
        };
      };
      users.users.operator = {
        isNormalUser = true;
        extraGroups = ["wheel"];
        openssh.authorizedKeys.keys = cfg.operatorKeys;
      };
      security.sudo.wheelNeedsPassword = false;
      networking.firewall.enable = true;
      environment.systemPackages = [adopt];

      # Writers start only after `terrace-production adopt`: a blank replacement
      # must not initialise an empty database before its backup is restored.
      systemd.targets.terrace-production = {
        description = "Terrace production writers (armed by terrace-production adopt)";
        requires = ["sops-install-secrets.service"];
        after = ["sops-install-secrets.service"];
      };
      systemd.services =
        lib.genAttrs cfg.writers (_: {
          wantedBy = lib.mkForce ["terrace-production.target"];
          serviceConfig.LimitCORE = 0;
        })
        // {
          terrace-production-boot = {
            description = "Start the Terrace writers on an adopted host whose secrets verify";
            wantedBy = ["multi-user.target"];
            after = ["sops-install-secrets.service"];
            unitConfig.ConditionPathExists = marker;
            serviceConfig = {
              Type = "oneshot";
              ExecStart = "${tool}/bin/terrace-secrets verify";
              ExecStartPost = "${pkgs.systemd}/bin/systemctl start --no-block terrace-production.target";
            };
          };
        };
      systemd.timers = lib.genAttrs cfg.writerTimers (_: {wantedBy = lib.mkForce ["terrace-production.target"];});
    }
  ]);
}
