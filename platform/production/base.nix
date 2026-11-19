# Shared production host policy for the app and relay roles: required
# parameters, operator SSH, the adoption boot gate and the invariants that make
# evaluation fail instead of deploying something unsafe. See
# docs/production-profile.md. The role modules import this file; the sops-nix
# module itself comes from the platform flake (nixosModules.production-*).
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.terrace.production;
  tool = pkgs.callPackage ./package.nix {};
  marker = "/var/lib/terrace-production/adopted";
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
  serviceEnvironmentLeaks = lib.concatLists (lib.mapAttrsToList (unit: service:
    lib.mapAttrsToList (name: _: "${unit}:${name}") (lib.filterAttrs (name: value:
      value
      != null
      && (secretLike name || lib.hasInfix "<SOPS:" (toString value)))
    service.environment))
  config.systemd.services);
  sopsItems = lib.attrValues config.sops.secrets ++ lib.attrValues config.sops.templates;
  rootOnly = item: item.mode == "0400" && item.owner == null && (item.uid == 0 || item.uid == null) && item.group == null && (item.gid == 0 || item.gid == null);
  optionalModuleOn = path: lib.attrByPath (path ++ ["enable"]) false config;
  adopt = pkgs.writeShellApplication {
    name = "terrace-production";
    runtimeInputs = [pkgs.coreutils pkgs.systemd tool];
    text = ''
      [[ $(id -u) == 0 ]] || { echo 'terrace-production: run as root' >&2; exit 1; }
      case "''${1:-}" in
        adopt)
          # Fail closed: the writers start only on a host whose secrets verify.
          terrace-secrets verify
          install -d -m 0700 /var/lib/terrace-production
          if [[ ! -e ${marker} ]]; then
            install -m 0600 /dev/null ${marker}
            date -u +%FT%TZ >${marker}
          fi
          systemctl start terrace-production.target
          # A writer whose dependency failed stays inactive, so report every failed unit.
          failed=$(systemctl list-units --state=failed --plain --no-legend | cut -d' ' -f1 | tr '\n' ' ')
          if [[ -n $failed ]]; then
            echo "Adopted, but units failed: $failed" >&2
            exit 1
          fi
          echo 'Adopted: the writers start now and at every boot.'
          ;;
        status)
          if [[ -e ${marker} ]]; then echo "adopted $(cat ${marker})"; else echo 'not adopted: no writer starts at boot'; fi
          systemctl is-active terrace-production.target || true
          ;;
        *) echo 'usage: terrace-production adopt|status' >&2; exit 2 ;;
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
          assertion = committedMiB <= cfg.memoryMiB && lib.all (unit: memoryOf unit != null) memoryUnits;
          message = "Terrace production writers commit ${toString committedMiB} MiB of MemoryMax (every writer needs one); the host declares ${toString cfg.memoryMiB} MiB.";
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
          assertion = serviceEnvironmentLeaks == [];
          message = "Terrace production units must not carry secrets in Environment=: ${toString serviceEnvironmentLeaks}.";
        }
        {
          assertion = config.networking.firewall.enable && lib.sort lib.lessThan (lib.unique config.networking.firewall.allowedTCPPorts) == lib.sort lib.lessThan cfg.publicTCPPorts && lib.unique config.networking.firewall.allowedUDPPorts == cfg.publicUDPPorts && config.networking.firewall.allowedTCPPortRanges == [] && config.networking.firewall.allowedUDPPortRanges == [] && config.networking.firewall.interfaces == {} && lib.subtractLists ["lo"] config.networking.firewall.trustedInterfaces == [];
          message = "Terrace production ${cfg.role} firewall must open exactly TCP ${toString cfg.publicTCPPorts} and UDP ${toString cfg.publicUDPPorts}.";
        }
      ];

      system.stateVersion = lib.mkDefault "26.05";
      services.openssh = {
        enable = true;
        ports = [22];
        settings = {
          PermitRootLogin = "no";
          PasswordAuthentication = false;
          KbdInteractiveAuthentication = false;
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
        lib.genAttrs cfg.writers (_: {wantedBy = lib.mkForce ["terrace-production.target"];})
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
