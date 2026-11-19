# Production profile invariants on fully evaluated Hetzner app and relay hosts
# and on the rehearsal instance, plus variants that must be rejected.
# No builds, provider calls, credentials or guest state. docs/production-profile.md
let
  platform = builtins.getFlake "path:${toString ../../platform}";
  lib = platform.inputs.nixpkgs.lib;
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  stub = pkgs.writeText "production-eval-only" "";
  pin = builtins.fromJSON (builtins.readFile ../../platform/website-release.json);
  release = {
    web = stub;
    cms = stub;
    inherit (pin) revision;
  };
  # The test-only public key the cloud check already uses.
  operatorKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEoUtCnNlZBsJyHCk+HjpH1L4sIv9N4G3mFVL/+UuUYQ";
  disk = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_terrace-eval-only";
  appParameters = {
    instance = "production";
    memoryMiB = 15360;
    operatorKeys = ["${operatorKey} eval-only"];
    app = {
      webHostname = "www.terracefclub.org";
      cmsHostname = "cms.terracefclub.org";
      identityHostname = "id.terracefclub.org";
      backup.primary = {
        repository = "sftp:terrace@backup.terracefclub.org:app";
        port = 23;
        knownHosts = "[backup.terracefclub.org]:23 ${operatorKey}";
      };
    };
  };
  evaluateApp = parameters: extra:
    (import ./production-config.nix {
      role = "app";
      inherit release;
      identityContract = stub;
      localModule = {
        imports = [extra];
        networking.hostName = "app";
        terrace.cloudDisk.device = disk;
        terrace.production = parameters;
      };
    }).config;
  app = evaluateApp appParameters;
  relay = extra:
    (import ./production-config.nix {
      role = "relay";
      localModule = {
        imports = [extra];
        networking.hostName = "relay";
        terrace.cloudDisk.device = disk;
        terrace.production = {
          instance = "production";
          memoryMiB = 3840;
          operatorKeys = ["${operatorKey} eval-only"];
          relay = {
            hostname = "headscale.terracefclub.org";
            publicIPv4 = "203.0.113.10";
            backup.primary = {
              repository = "sftp:terrace@backup.terracefclub.org:relay";
              port = 23;
              knownHosts = "[backup.terracefclub.org]:23 ${operatorKey}";
            };
          };
        };
      };
    }).config;
  # The VM proof's instance: same role, synthetic names, the rehearsal harness.
  rehearsal = extra:
    app {
      imports = [(import ../../platform/production/rehearsal.nix {ca = stub;}) extra];
      terrace.production.instance = lib.mkForce "rehearsal";
      terrace.production.app = {
        webHostname = lib.mkForce "web.terrace.invalid";
        cmsHostname = lib.mkForce "cms.terrace.invalid";
        identityHostname = lib.mkForce "identity.terrace.invalid";
      };
    };

  inventory = (builtins.fromJSON (builtins.readFile ../../platform/production/inventory.json)).secrets;
  delivered = role: lib.sort lib.lessThan (map (entry: entry.name) (lib.filter (entry: builtins.elem role entry.roles && entry.delivered && entry.feature == "core") inventory));
  # Operating-system units allowed to start at boot, each for the stated reason.
  # Everything that writes application state is wanted by terrace-production.target.
  allowedBoot = {
    services = [
      "sops-install-secrets" # decrypts this host's secrets into ramfs
      "terrace-production-boot" # starts the writers only on an adopted host
      "sshd" # operator access, key-only
      "sshd-keygen" # no-op: the installer staged the only host key
      "firewall"
      # Stock NixOS plumbing; none touches application state.
      "dhcpcd"
      "resolvconf"
      "network-local-commands"
      "nscd"
      "systemd-timesyncd"
      "systemd-oomd"
      "systemd-pstore"
      "systemd-sysctl"
      "systemd-modules-load"
      "systemd-tmpfiles-resetup"
      "reload-systemd-vconsole-setup"
      "suid-sgid-wrappers"
      "post-boot"
      "prepare-kexec"
      "generate-shutdown-ramfs"
      "sleep-actions"
      "linger-users"
      "lastlog2-import"
      "logrotate-checkconf"
    ];
    timers = ["fstrim" "logrotate"];
    sockets = ["nix-daemon" "systemd-journald-audit"];
    paths = [];
  };
  wanted = cfg: kind: name: cfg.systemd.${kind}.${name}.wantedBy ++ (cfg.systemd.${kind}.${name}.requiredBy or []) ++ (cfg.systemd.${kind}.${name}.upheldBy or []);
  bootUnits = cfg: kind: lib.filter (name: let w = wanted cfg kind name; in w != [] && w != ["terrace-production.target"]) (lib.attrNames cfg.systemd.${kind});
  gatedUnits = cfg: kind: lib.sort lib.lessThan (lib.filter (name: wanted cfg kind name == ["terrace-production.target"]) (lib.attrNames cfg.systemd.${kind}));
  sorted = lib.sort lib.lessThan;
  check = label: condition: lib.optional (!condition) label;
  violations = role: cfg: let
    p = cfg.terrace.production;
    services = cfg.systemd.services;
    sops = lib.attrValues cfg.sops.secrets ++ lib.attrValues cfg.sops.templates;
  in
    map (a: "assertion: " + a.message) (lib.filter (a: !a.assertion) cfg.assertions)
    # The rehearsal harness adds exactly one boot unit: the installer stand-in.
    ++ check "boot services: ${toString (bootUnits cfg "services")}" (sorted (bootUnits cfg "services") == sorted (allowedBoot.services ++ lib.optional (p.instance == "rehearsal") "terrace-rehearsal-stage"))
    ++ check "boot timers: ${toString (bootUnits cfg "timers")}" (sorted (bootUnits cfg "timers") == sorted allowedBoot.timers)
    ++ check "boot sockets: ${toString (bootUnits cfg "sockets")}" (sorted (bootUnits cfg "sockets") == sorted allowedBoot.sockets)
    ++ check "boot paths: ${toString (bootUnits cfg "paths")}" (bootUnits cfg "paths" == allowedBoot.paths)
    ++ check "gated services: ${toString (gatedUnits cfg "services")}" (gatedUnits cfg "services" == sorted p.writers)
    ++ check "gated timers: ${toString (gatedUnits cfg "timers")}" (gatedUnits cfg "timers" == sorted p.writerTimers)
    ++ check "firewall" (cfg.networking.firewall.enable && sorted (lib.unique cfg.networking.firewall.allowedTCPPorts) == [22 443] && lib.unique cfg.networking.firewall.allowedUDPPorts == (lib.optional (role == "relay") 3478))
    ++ check "ssh policy" (cfg.services.openssh.enable && cfg.services.openssh.ports == [22] && cfg.services.openssh.settings.PermitRootLogin == "no" && !cfg.services.openssh.settings.PasswordAuthentication && !cfg.services.openssh.settings.KbdInteractiveAuthentication && map (k: k.type) cfg.services.openssh.hostKeys == ["ed25519"])
    ++ check "secret inventory: ${toString (lib.attrNames cfg.sops.secrets)}" (p.instance != "production" || sorted (lib.attrNames cfg.sops.secrets) == delivered role)
    ++ check "world-readable or non-root secret" (lib.all (item: item.mode == "0400" && item.owner == null && item.group == null) sops)
    ++ check "ciphertext outside the store" (cfg.sops.defaultSopsFile == "/var/lib/terrace-secrets/secrets.sops.json" && !cfg.sops.validateSopsFiles)
    ++ check "test-only override: extra trust or CA variables" (p.instance != "production" || (cfg.security.pki.certificateFiles == [] && lib.all (service: !(service.environment ? NODE_EXTRA_CA_CERTS)) (lib.attrValues services)))
    ++ check "bootstrap administrator" (p.instance != "production" || role != "app" || !(services.keycloak.serviceConfig ? EnvironmentFile))
    ++ check "K3s" (!cfg.services.k3s.enable)
    ++ check "toplevel does not evaluate" ((builtins.tryEval cfg.system.build.toplevel.drvPath).success && (builtins.tryEval cfg.system.build.diskoScript.drvPath).success)
    ++ lib.optionals (role == "app") (
      check "static ids" (cfg.users.users.terrace_cms.uid == 44121 && cfg.users.users.terrace_web.uid == 44122)
      ++ check "loopback services" (builtins.elem cfg.services.postgresql.settings.listen_addresses ["127.0.0.1" "localhost"] && cfg.services.keycloak.settings.http-host == "127.0.0.1" && cfg.services.keycloak.settings.http-management-host == "127.0.0.1" && !(builtins.elem cfg.terrace.website.webPort cfg.networking.firewall.allowedTCPPorts) && !(builtins.elem cfg.terrace.website.cmsPort cfg.networking.firewall.allowedTCPPorts))
      ++ check "sweep and backup timers" (builtins.elem "terrace-cms-sweep" (gatedUnits cfg "timers") && builtins.elem "terrace-backup" (gatedUnits cfg "timers") && cfg.systemd.timers.terrace-cms-sweep.timerConfig.Persistent)
      ++ check "env files from templates" (cfg.terrace.website.inputFiles.cms == "/run/secrets/rendered/terrace-website/cms.env" && services.terrace-cms.serviceConfig.EnvironmentFile == "/run/secrets/rendered/terrace-website/cms.env" && services.terrace-web.serviceConfig.EnvironmentFile == "/run/secrets/rendered/terrace-website/web.env")
      ++ check "backup excludes secrets" (!(cfg.terrace.backup.extraPaths ? website-inputs) && lib.all (path: !(lib.hasPrefix "/run" path) && !(lib.hasPrefix "/var/lib/terrace-secrets" path)) (lib.attrValues cfg.terrace.backup.extraPaths))
    )
    ++ lib.optionals (role == "relay") (
      check "relay loopback" (cfg.services.headscale.address == "127.0.0.1")
      ++ check "relay headscale backup" cfg.terrace.backup.headscale.enable
    );
  rejects = needle: cfg: let v = violations "app" cfg; in lib.assertMsg (lib.any (lib.hasInfix needle) v) "Variant not rejected for ${needle}: ${toString v}";
  rejectsRelay = needle: cfg: let v = violations "relay" cfg; in lib.assertMsg (lib.any (lib.hasInfix needle) v) "Relay variant not rejected for ${needle}: ${toString v}";
  fails = label: cfg: lib.assertMsg (!(builtins.tryEval cfg.system.build.toplevel.drvPath).success) "Variant evaluated but must fail: ${label}";
  positive = {
    app = violations "app" (app {});
    relay = violations "relay" (relay {});
    rehearsal = violations "app" (rehearsal {});
  };
  negatives = [
    # A required secret missing from the declared set.
    (rejects "secret(s) not declared: website/lifecycle-intake" (app ({config, ...}: {
      sops.secrets = lib.mkForce (lib.genAttrs (lib.remove "website/lifecycle-intake" config.terrace.production.secrets.required) (_: {mode = "0400";}));
    })))
    # A required parameter missing: evaluation stops.
    (fails "missing knownHosts" (evaluateApp (appParameters // {app = appParameters.app // {backup.primary = removeAttrs appParameters.app.backup.primary ["knownHosts"];};}) {}))
    (fails "missing instance" (evaluateApp (removeAttrs appParameters ["instance"]) {}))
    (fails "missing memoryMiB" (evaluateApp (removeAttrs appParameters ["memoryMiB"]) {}))
    # Test fixtures imported into a production instance.
    (rejects "refuses the rehearsal harness" (app (import ../../platform/production/rehearsal.nix {ca = stub;})))
    (rejects "refuses test fixture units: gateway-test-inputs" (app {systemd.services.gateway-test-inputs.script = "true";}))
    (rejects "test-only override" (app {systemd.services.terrace-web.environment.NODE_EXTRA_CA_CERTS = "/etc/ssl/rehearsal.pem";}))
    # A synthetic hostname in production mode.
    (rejects "refuses a synthetic or reserved hostname (web.terrace.invalid)" (app {terrace.production.app.webHostname = lib.mkForce "web.terrace.invalid";}))
    (rejects "refuses a synthetic or reserved hostname (cms.example.org)" (app {terrace.production.app.cmsHostname = lib.mkForce "cms.example.org";}))
    # Unexpected boot units.
    (rejects "boot services: " (app {
      systemd.services.rogue-writer = {
        wantedBy = ["multi-user.target"];
        script = "true";
      };
    }))
    (rejects "boot timers: " (app {systemd.timers.terrace-cms-sweep.wantedBy = lib.mkOverride 10 ["timers.target"];}))
    (rejects "gated services: " (app {systemd.services.keycloak.wantedBy = lib.mkOverride 10 ["multi-user.target"];}))
    # Bootstrap and registration flags.
    (rejects "refuses a Keycloak bootstrap administrator" (app {systemd.services.keycloak.serviceConfig.EnvironmentFile = "/run/bootstrap.env";}))
    (fails "initialAdminPassword" (app {services.keycloak.initialAdminPassword = "eval-only-placeholder";}))
    (rejects "must not contain CMS bootstrap settings" (app {sops.templates."terrace-website/cms.env".content = lib.mkAfter "CMS_BOOTSTRAP_ADMIN=1\n";}))
    # Public binds that the design keeps private.
    (rejects "keeps PostgreSQL and Keycloak (HTTP and management) on loopback" (app {services.postgresql.settings.listen_addresses = lib.mkForce "*";}))
    (rejects "firewall must open exactly" (app {networking.firewall.allowedTCPPorts = [5432];}))
    (rejectsRelay "keeps Headscale's API, metrics and gRPC on loopback" (relay {services.headscale.settings.metrics_listen_addr = lib.mkForce "0.0.0.0:9090";}))
    # Secret handling.
    (rejects "must be root:root 0400" (app {sops.secrets."tls/key".mode = lib.mkForce "0444";}))
    (rejects "consumers must read sops-nix paths" (app {terrace.backup.targets.primary.passwordFile = lib.mkForce "/root/repository-password";}))
    (rejects "must not carry secrets in Environment=" (app {systemd.services.terrace-web.environment.EXTRA_SECRET = "eval-only";}))
    (rejects "refuses unencrypted swap" (app {swapDevices = [{device = "/dev/disk/by-label/swap";}];}))
    # Capacity and scope.
    (rejects "commit 6912 MiB" (evaluateApp (appParameters // {memoryMiB = 4096;}) {}))
    (rejects "does not yet enable K3s" (app {services.k3s.enable = true;}))
    (rejectsRelay "public IPv4" (relay {terrace.production.relay.publicIPv4 = lib.mkForce "10.0.0.8";}))
    # Hardware: production needs a root filesystem and an installable boot loader.
    (rejects "per-host hardware module" (app {boot.loader.systemd-boot.enable = lib.mkForce false;}))
  ];
in
  assert lib.assertMsg (positive.app == []) "Production app violations: ${toString positive.app}";
  assert lib.assertMsg (positive.relay == []) "Production relay violations: ${toString positive.relay}";
  assert lib.assertMsg (positive.rehearsal == []) "Rehearsal instance violations: ${toString positive.rehearsal}";
  assert lib.all lib.id negatives; "PASS: production app, relay and rehearsal instance hold every invariant; ${toString (builtins.length negatives)} rejected variants"
