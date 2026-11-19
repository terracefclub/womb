# Production profile invariants on fully evaluated Hetzner app and relay hosts,
# a static-network host and the rehearsal instance, plus variants that must be
# rejected, each for the stated reason. No builds, provider calls, credentials
# or guest state. docs/production-profile.md
let
  install = builtins.getFlake "path:${toString ./.}";
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
  # Syntactically valid placeholder recipient (no private key exists for it).
  escrowRecipient = "age1" + lib.concatStrings (lib.replicate 58 "q");
  disk = "/dev/disk/by-id/scsi-0QEMU_QEMU_HARDDISK_terrace-eval-only";
  hardwareModule = {
    imports = [install.nixosModules.cloud-disk];
    terrace.cloudDisk.device = disk;
  };
  appParameters = {
    instance = "production";
    memoryMiB = 15360;
    operatorKeys = ["${operatorKey} eval-only"];
    inherit escrowRecipient;
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
    import ./production-config.nix {
      role = "app";
      inherit release hardwareModule;
      identityContract = stub;
      localModule = {
        imports = [extra];
        networking.hostName = "app";
        terrace.production = parameters;
      };
    };
  app = extra: evaluateApp appParameters extra;
  relayParameters = {
    instance = "production";
    memoryMiB = 3840;
    operatorKeys = ["${operatorKey} eval-only"];
    inherit escrowRecipient;
    relay = {
      hostname = "headscale.terracefclub.org";
      # A documentation address: only the rehearsal instance accepts it (see `relayDocumentation`).
      publicIPv4 = "203.0.113.10";
      backup.primary = {
        repository = "sftp:terrace@backup.terracefclub.org:relay";
        port = 23;
        knownHosts = "[backup.terracefclub.org]:23 ${operatorKey}";
      };
    };
  };
  evaluateRelay = parameters: extra:
    import ./production-config.nix {
      role = "relay";
      inherit hardwareModule;
      localModule = {
        imports = [extra];
        networking.hostName = "relay";
        terrace.production = parameters;
      };
    };
  relayAs = instance: extra: evaluateRelay (relayParameters // {inherit instance;}) extra;
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
  # An on-prem-style host with static addressing instead of DHCP.
  staticNetwork = {
    networking.useDHCP = false;
    networking.interfaces.eth0.ipv4.addresses = [
      {
        address = "192.0.2.20";
        prefixLength = 24;
      }
    ];
    networking.defaultGateway = "192.0.2.1";
  };

  inventory = (builtins.fromJSON (builtins.readFile ../../platform/production/inventory.json)).secrets;
  delivered = role: lib.sort lib.lessThan (map (entry: entry.name) (lib.filter (entry: builtins.elem role entry.roles && entry.delivered && entry.feature == "core") inventory));
  # Units allowed to start at boot, each for the stated reason; everything that
  # writes application state is wanted by terrace-production.target instead.
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
      "network-setup"
      "network-addresses-eth0"
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
    required = ["sops-install-secrets" "terrace-production-boot" "sshd" "firewall"];
    timers = ["fstrim" "logrotate"];
    sockets = ["nix-daemon" "systemd-journald-audit"];
    paths = [];
    targets = [];
    # Pulled in by NixOS's multi-user.target; no unit of ours.
    targetPulls = ["machines.target"];
  };
  # Services that may be enabled at all (each listens only where the checks above say), and socket units.
  allowedEnabled = role:
    [
      "openssh" # operator access (port 22)
      "nginx" # the HTTPS front (443; 80 redirect on the relay)
      "nscd" # name-service cache, local socket only
      "timesyncd" # NTP client, no listener
      "logrotate" # rotates local logs, no listener
      "dhcpcd" # address configuration client (UDP 68/546 client sockets)
      "resolved" # local stub resolver on 127.0.0.53 when enabled
      "userborn" # creates system users at boot, no listener
      "lvm" # disk tooling, no listener
      "udisks2" # disk tooling (system bus only)
      "fstrim" # periodic TRIM, no listener
      "dbus" # system bus, local socket only
      "getty" # console login (no password exists on any account)
      "logind" # session management, no listener
      "udev" # device manager, no listener
    ]
    ++ (
      if role == "app"
      then [
        "postgresql" # loopback 5432 and a local socket
        "keycloak" # loopback 8088 and 9000
      ]
      else [
        "headscale" # loopback 8080, 9090, 50443; STUN UDP 3478
      ]
    );
  allowedSockets = ["nix-daemon" "systemd-journald-audit"];
  # Units the profile's own and NixOS's allowed units may pull in through wants/requires/bindsTo/upholds.
  allowedPulls = role:
    ["network-online.target" "network.target" "network-pre.target" "systemd-logind.service" "sysinit.target" "multi-user.target" "local-fs.target" "remote-fs.target" "nss-lookup.target" "nss-user-lookup.target" "time-sync.target" "systemd-journald.socket" "sops-install-secrets.service" "terrace-production.target" "systemd-journald-sync@terrace-apps.service" "systemd-journald@terrace-apps.service"]
    ++ (
      if role == "app"
      then ["postgresql.service" "postgresql.target" "postgresql-setup.service" "keycloakPostgreSQLInit.service" "terrace-website-inputs.service" "terrace-cms.service" "terrace-cms-migrate.service" "keycloak.service"]
      else ["headscale.service"]
    );
  # Packages that bring their own unit files, accepted for the stated reason.
  allowedSystemdPackages = [
    "lvm2" # NixOS default: device-mapper units
    "nix" # the Nix daemon's units
    "dbus" # system bus units
    "dbus-broker" # the system bus implementation
    "util-linux" # fstrim and uuidd units (listed twice by NixOS)
  ];
  bootTargets = ["multi-user" "default" "sysinit" "basic" "timers" "sockets" "paths" "network" "network-online" "local-fs" "graphical"];
  wanted = cfg: kind: name: cfg.systemd.${kind}.${name}.wantedBy ++ (cfg.systemd.${kind}.${name}.requiredBy or []) ++ (cfg.systemd.${kind}.${name}.upheldBy or []);
  bootUnits = cfg: kind: lib.filter (name: let w = wanted cfg kind name; in w != [] && w != ["terrace-production.target"]) (lib.attrNames cfg.systemd.${kind});
  gatedUnits = cfg: kind: lib.sort lib.lessThan (lib.filter (name: wanted cfg kind name == ["terrace-production.target"]) (lib.attrNames cfg.systemd.${kind}));
  # Units pulled in at boot from the target side (systemd.targets.<boot>.wants/requires).
  targetPulls = cfg: lib.concatMap (target: let t = cfg.systemd.targets.${target} or {}; in (t.wants or []) ++ (t.requires or [])) bootTargets;
  sorted = lib.sort lib.lessThan;
  subset = small: big: lib.all (item: builtins.elem item big) small;
  check = label: condition: lib.optional (!condition) label;
  storePath = value: lib.hasPrefix builtins.storeDir (lib.removePrefix "-" (toString value));
  lockedPassword = value: value == null || builtins.elem value ["!" "!*" "*"];
  passwordless = user: lockedPassword user.hashedPassword && user.password == null && lockedPassword user.initialHashedPassword && user.initialPassword == null && user.hashedPasswordFile == null;
  violations = role: system: let
    cfg = system.config;
    p = cfg.terrace.production;
    services = cfg.systemd.services;
    sops = lib.attrValues cfg.sops.secrets ++ lib.attrValues cfg.sops.templates;
    bootServices = bootUnits cfg "services";
    allowedServices = allowedBoot.services ++ lib.optional (p.instance == "rehearsal") "terrace-rehearsal-stage" ++ map (name: "network-addresses-${name}") (lib.attrNames cfg.networking.interfaces);
    users = lib.attrValues cfg.users.users;
    wheel = lib.unique (map (user: user.name) (lib.filter (user: builtins.elem "wheel" user.extraGroups || user.group == "wheel") users) ++ cfg.users.groups.wheel.members);
    firstError = let
      failed = lib.filter (a: !a.assertion) cfg.assertions;
    in
      if failed == []
      then "a non-assertion error (evaluate the toplevel for its trace)"
      else (builtins.head failed).message;
    # From option metadata: reading every service's value would trip renamed-option aliases.
    enabledServices = lib.filter (name: let option = system.options.services.${name}.enable or null; in lib.isOption option && option.isDefined && option.value == true) (lib.attrNames system.options.services);
    # wants/requires/bindsTo/upholds of every unit allowed at boot or gated by the production target.
    profileUnits = map (name: name + ".service") (allowedServices ++ p.writers ++ p.writerHelpers);
    pullsOf = service: (service.wants or []) ++ (service.requires or []) ++ (service.bindsTo or []) ++ (service.upholds or []);
    interfaceDevices = map (name: "sys-subsystem-net-devices-${name}.device") (lib.attrNames cfg.networking.interfaces);
    strayPulls = lib.unique (lib.subtractLists (profileUnits ++ allowedPulls role ++ interfaceDevices) (lib.concatMap (name: pullsOf (cfg.systemd.services.${name} or {})) (allowedServices ++ p.writers ++ p.writerHelpers)));
    etcUnits = lib.filter (name: lib.hasPrefix "systemd/system/" name || lib.hasPrefix "systemd/user/" name) (lib.attrNames cfg.environment.etc);
    socketUnits = lib.filter (name: (cfg.systemd.sockets.${name}.listenStreams or []) != [] || (cfg.systemd.sockets.${name}.listenDatagrams or []) != []) (lib.attrNames cfg.systemd.sockets);
    unitFiles = lib.concatLists (lib.mapAttrsToList (unit: service: let
      env = lib.toList (service.serviceConfig.EnvironmentFile or []);
      credentials = map (entry: lib.last (lib.splitString ":" entry)) (lib.toList (service.serviceConfig.LoadCredential or []));
    in
      map (path: "${unit}:${toString path}") (lib.filter storePath (env ++ credentials)))
    services);
    vhosts = cfg.services.nginx.virtualHosts;
    expectedVhosts =
      if role == "app"
      then p.publicHostnames ++ ["terrace-reject-unknown"]
      else p.publicHostnames;
  in
    map (a: "assertion: " + a.message) (lib.filter (a: !a.assertion) cfg.assertions)
    ++ check "boot services: ${toString (lib.subtractLists allowedServices bootServices)}" (subset bootServices allowedServices && subset allowedBoot.required bootServices)
    ++ check "boot timers: ${toString (bootUnits cfg "timers")}" (sorted (bootUnits cfg "timers") == sorted allowedBoot.timers)
    ++ check "boot sockets: ${toString (bootUnits cfg "sockets")}" (sorted (bootUnits cfg "sockets") == sorted allowedBoot.sockets)
    ++ check "boot paths: ${toString (bootUnits cfg "paths")}" (bootUnits cfg "paths" == allowedBoot.paths)
    ++ check "boot targets: ${toString (lib.subtractLists bootTargets (bootUnits cfg "targets"))}" (lib.subtractLists bootTargets (bootUnits cfg "targets") == allowedBoot.targets)
    ++ check "boot target wants: ${toString (lib.subtractLists allowedBoot.targetPulls (targetPulls cfg))}" (subset (targetPulls cfg) allowedBoot.targetPulls)
    ++ check "gated services: ${toString (gatedUnits cfg "services")}" (gatedUnits cfg "services" == sorted p.writers)
    ++ check "gated timers: ${toString (gatedUnits cfg "timers")}" (gatedUnits cfg "timers" == sorted p.writerTimers)
    ++ check "firewall" (cfg.networking.firewall.enable
      && sorted (lib.unique cfg.networking.firewall.allowedTCPPorts)
      == (
        if role == "relay"
        then [22 80 443]
        else [22 443]
      )
      && lib.unique cfg.networking.firewall.allowedUDPPorts == (lib.optional (role == "relay") 3478))
    ++ check "ssh policy" (cfg.services.openssh.enable && cfg.services.openssh.ports == [22] && cfg.services.openssh.settings.PermitRootLogin == "no" && !cfg.services.openssh.settings.PasswordAuthentication && !cfg.services.openssh.settings.KbdInteractiveAuthentication && cfg.services.openssh.settings.PermitEmptyPasswords == false && cfg.services.openssh.settings.AllowUsers == ["operator"] && map (k: k.type) cfg.services.openssh.hostKeys == ["ed25519"])
    ++ check "accounts: mutable users" (!cfg.users.mutableUsers)
    ++ check "accounts: wheel members ${toString wheel}" (wheel == ["operator"])
    ++ check "accounts: password field on ${toString (map (user: user.name) (lib.filter (user: !(passwordless user)) users))}" (lib.all passwordless users)
    ++ check "enabled services: ${toString (lib.subtractLists (allowedEnabled role) enabledServices)}" (subset enabledServices (allowedEnabled role))
    ++ check "listening sockets: ${toString (lib.subtractLists allowedSockets socketUnits)}" (subset socketUnits allowedSockets)
    ++ check "unit dependencies: ${toString strayPulls}" (strayPulls == [])
    ++ check "systemd packages: ${toString (map lib.getName cfg.systemd.packages)}" (subset (map lib.getName cfg.systemd.packages) allowedSystemdPackages)
    ++ check "unit files through environment.etc: ${toString etcUnits}" (etcUnits == [])
    ++ check "container or VM daemons" (!(lib.any (name: lib.attrByPath [name "enable"] false cfg.virtualisation) ["docker" "podman" "libvirtd" "containers" "incus" "cri-o"]))
    ++ check "sudo rules" (lib.all (rule: subset (rule.users or []) ["root"] && subset (rule.groups or []) ["wheel"]) cfg.security.sudo.extraRules)
    ++ check "store paths in EnvironmentFile or LoadCredential: ${toString unitFiles}" (unitFiles == [])
    ++ check "kernel debug or emergency access" (!cfg.systemd.enableEmergencyMode && !(lib.any (param: builtins.elem param ["debug" "single" "emergency" "rd.debug" "systemd.debug-shell" "systemd.debug_shell"] || lib.hasPrefix "init=" param || lib.hasPrefix "systemd.unit=" param || lib.hasPrefix "rd.systemd.unit=" param) cfg.boot.kernelParams))
    ++ check "nginx vhosts: ${toString (lib.attrNames vhosts)}" (sorted (lib.attrNames vhosts)
      == sorted expectedVhosts
      && lib.all (vhost:
        (
          if role == "relay"
          then vhost.forceSSL && !vhost.onlySSL
          else vhost.onlySSL && !vhost.forceSSL
        )
        && !vhost.addSSL
        && !vhost.enableACME
        && vhost.listen == []
        && vhost.listenAddresses == []) (lib.attrValues vhosts))
    ++ check "core dumps" (!cfg.systemd.coredump.enable && lib.hasSuffix "/bin/false" cfg.boot.kernel.sysctl."kernel.core_pattern")
    ++ check "secret inventory: ${toString (lib.attrNames cfg.sops.secrets)}" (p.instance != "production" || sorted (lib.attrNames cfg.sops.secrets) == delivered role)
    ++ check "world-readable or non-root secret" (lib.all (item: item.mode == "0400" && item.owner == null && item.group == null) sops)
    ++ check "ciphertext outside the store" (cfg.sops.defaultSopsFile == "/var/lib/terrace-secrets/secrets.sops.json" && !cfg.sops.validateSopsFiles && cfg.environment.etc."terrace/escrow-recipient".text == escrowRecipient + "\n")
    ++ check "test-only override: extra trust or CA variables" (p.instance != "production" || (cfg.security.pki.certificateFiles == [] && lib.all (service: !(service.environment ? NODE_EXTRA_CA_CERTS)) (lib.attrValues services)))
    ++ check "bootstrap administrator" (p.instance != "production" || role != "app" || !(services.keycloak.serviceConfig ? EnvironmentFile))
    ++ check "K3s" (!cfg.services.k3s.enable)
    ++ check "toplevel does not evaluate: ${firstError}" ((builtins.tryEval cfg.system.build.toplevel.drvPath).success)
    ++ lib.optionals (role == "app") (
      check "static ids" (cfg.users.users.terrace_cms.uid == 44121 && cfg.users.users.terrace_web.uid == 44122)
      # Listening sockets, from the evaluated configuration: only nginx (443) and sshd (22) on public addresses.
      ++ check "listeners" (!(lib.any (assignment: lib.hasPrefix "HOST=" assignment || lib.hasPrefix "HOSTNAME=" assignment) (lib.concatMap (unit: lib.concatMap (item: lib.splitString " " (toString item)) (lib.toList (services.${unit}.serviceConfig.Environment or []))) ["terrace-cms" "terrace-web"])) && lib.all (template: !(lib.any (line: lib.hasPrefix "HOST=" line || lib.hasPrefix "HOSTNAME=" line) (lib.splitString "\n" cfg.sops.templates.${template}.content))) ["terrace-website/cms.env" "terrace-website/web.env"] && !(services.terrace-cms.environment ? HOST) && !(services.terrace-web.environment ? HOST) && !(services.terrace-cms.environment ? HOSTNAME) && !(services.terrace-web.environment ? HOSTNAME) && builtins.elem cfg.services.postgresql.settings.listen_addresses ["127.0.0.1" "localhost"] && cfg.services.keycloak.settings.http-host == "127.0.0.1" && cfg.services.keycloak.settings.http-management-host == "127.0.0.1" && !(cfg.services.keycloak.settings ? https-certificate-file) && services.terrace-cms.environment.PORT == toString cfg.terrace.website.cmsPort && services.terrace-web.environment.PORT == toString cfg.terrace.website.webPort && !(builtins.elem cfg.terrace.website.webPort cfg.networking.firewall.allowedTCPPorts) && !(builtins.elem cfg.terrace.website.cmsPort cfg.networking.firewall.allowedTCPPorts))
      ++ check "Keycloak database init logs no statements" (services.keycloakPostgreSQLInit.environment.PGOPTIONS or "" == "-c log_min_error_statement=panic -c log_statement=none -c log_min_duration_statement=-1" && lib.hasInfix "terrace-keycloak-init-psqlrc" (services.keycloakPostgreSQLInit.environment.PSQLRC or ""))
      ++ check "sweep and backup timers" (builtins.elem "terrace-cms-sweep" (gatedUnits cfg "timers") && builtins.elem "terrace-backup" (gatedUnits cfg "timers") && cfg.systemd.timers.terrace-cms-sweep.timerConfig.Persistent)
      ++ check "env files from templates" (cfg.terrace.website.inputFiles.cms == "/run/secrets/rendered/terrace-website/cms.env" && services.terrace-cms.serviceConfig.EnvironmentFile == "/run/secrets/rendered/terrace-website/cms.env" && services.terrace-web.serviceConfig.EnvironmentFile == "/run/secrets/rendered/terrace-website/web.env")
      ++ check "backup excludes secrets" (!(cfg.terrace.backup.extraPaths ? website-inputs) && lib.all (path: !(lib.hasPrefix "/run" path) && !(lib.hasPrefix "/var/lib/terrace-secrets" path)) (lib.attrValues cfg.terrace.backup.extraPaths))
    )
    ++ lib.optionals (role == "relay") (
      check "relay listeners" (cfg.services.headscale.address == "127.0.0.1" && lib.hasPrefix "127.0.0.1:" cfg.services.headscale.settings.metrics_listen_addr && lib.hasPrefix "127.0.0.1:" cfg.services.headscale.settings.grpc_listen_addr)
      ++ check "relay headscale backup" cfg.terrace.backup.headscale.enable
    );
  has = needle: list: lib.any (lib.hasInfix needle) list;
  rejects = needle: cfg: let v = violations "app" cfg; in lib.assertMsg (has needle v) "Variant not rejected for ${needle}: ${toString v}";
  rejectsRelay = needle: cfg: let v = violations "relay" cfg; in lib.assertMsg (has needle v) "Relay variant not rejected for ${needle}: ${toString v}";
  # A missing required parameter stops evaluation: assert it fails AND that exactly that option is undefined.
  failsMissing = path: system:
    lib.assertMsg (!(builtins.tryEval system.config.system.build.toplevel.drvPath).success && !(lib.attrByPath path {isDefined = true;} system.options).isDefined)
    "Missing ${lib.concatStringsSep "." path} not reported";
  positive = {
    app = violations "app" (app {});
    static = violations "app" (app staticNetwork);
    relayRehearsal = violations "relay" (relayAs "rehearsal" {});
    rehearsal = violations "app" (rehearsal {});
  };
  # The production relay with a documentation address must fail for that reason alone,
  # which proves every other relay invariant in production mode without a real address.
  relayDocumentation = violations "relay" (relayAs "production" {});
  negatives = [
    # Secrets.
    (rejects "secret(s) not declared: website/lifecycle-intake" (app ({config, ...}: {
      sops.secrets = lib.mkForce (lib.genAttrs (lib.remove "website/lifecycle-intake" config.terrace.production.secrets.required) (_: {mode = "0400";}));
    })))
    (rejects "must be root:root 0400" (app {sops.secrets."tls/key".mode = lib.mkForce "0444";}))
    (rejects "only from the host ciphertext" (app {sops.secrets."tls/key".sopsFile = lib.mkForce "/var/lib/elsewhere.json";}))
    (rejects "consumers must read sops-nix paths" (app {terrace.backup.targets.primary.passwordFile = lib.mkForce "/root/repository-password";}))
    (rejectsRelay "consumers must read sops-nix paths" (relayAs "production" {systemd.services.nginx.serviceConfig.LoadCredential = lib.mkForce ["terrace-relay-key:/root/key.pem"];}))
    (rejects "must not carry secrets in Environment=" (app {systemd.services.terrace-web.environment.EXTRA_SECRET = "eval-only";}))
    (rejects "must not carry secrets in Environment=" (app {systemd.services.terrace-web.serviceConfig.Environment = "DB_PASSWORD=eval-only";}))
    (rejects "writes no core dumps" (app {systemd.coredump.enable = lib.mkForce true;}))
    (rejects "core dumps" (app {boot.kernel.sysctl."kernel.core_pattern" = lib.mkOverride 10 "core";}))
    (rejects "refuses unencrypted swap" (app {swapDevices = [{device = "/dev/disk/by-label/swap";}];}))
    (rejects "store paths in EnvironmentFile or LoadCredential" (app {systemd.services.nscd.serviceConfig.EnvironmentFile = "${stub}";}))
    (failsMissing ["terrace" "production" "escrowRecipient"] (evaluateApp (removeAttrs appParameters ["escrowRecipient"]) {}))
    (rejects "escrow key's public age recipient" (evaluateApp (appParameters // {escrowRecipient = "age1short";}) {}))
    # Required parameters.
    (failsMissing ["terrace" "production" "app" "backup" "primary" "knownHosts"] (evaluateApp (appParameters // {app = appParameters.app // {backup.primary = removeAttrs appParameters.app.backup.primary ["knownHosts"];};}) {}))
    (failsMissing ["terrace" "production" "instance"] (evaluateApp (removeAttrs appParameters ["instance"]) {}))
    (failsMissing ["terrace" "production" "memoryMiB"] (evaluateApp (removeAttrs appParameters ["memoryMiB"]) {}))
    (failsMissing ["terrace" "production" "app" "webHostname"] (evaluateApp (appParameters // {app = removeAttrs appParameters.app ["webHostname"];}) {}))
    # Test fixtures in a production instance.
    (rejects "refuses the rehearsal harness" (app (import ../../platform/production/rehearsal.nix {ca = stub;})))
    (rejects "refuses test fixture units: gateway-test-inputs" (app {systemd.services.gateway-test-inputs.script = "true";}))
    (rejects "test-only override" (app {systemd.services.terrace-web.environment.NODE_EXTRA_CA_CERTS = "/etc/ssl/rehearsal.pem";}))
    # Synthetic hostnames and addresses in production mode.
    (rejects "refuses a synthetic or reserved hostname (web.terrace.invalid)" (app {terrace.production.app.webHostname = lib.mkForce "web.terrace.invalid";}))
    (rejects "refuses a synthetic or reserved hostname (cms.example.org)" (app {terrace.production.app.cmsHostname = lib.mkForce "cms.example.org";}))
    (rejectsRelay "public IPv4" (relayAs "production" {terrace.production.relay.publicIPv4 = lib.mkForce "10.0.0.8";}))
    # Boot units, gated units and target pulls.
    (rejects "boot services: rogue-writer" (app {
      systemd.services.rogue-writer = {
        wantedBy = ["multi-user.target"];
        script = "true";
      };
    }))
    (rejects "boot timers: " (app {systemd.timers.terrace-cms-sweep.wantedBy = lib.mkOverride 10 ["timers.target"];}))
    (rejects "gated services: " (app {systemd.services.keycloak.wantedBy = lib.mkOverride 10 ["multi-user.target"];}))
    (rejects "boot target wants: rogue.service" (app {systemd.targets.multi-user.wants = ["rogue.service"];}))
    (rejects "boot targets: terrace-production" (app {systemd.targets.terrace-production.wantedBy = ["multi-user.target"];}))
    # Bootstrap and registration flags.
    (rejects "refuses a Keycloak bootstrap administrator" (app {systemd.services.keycloak.serviceConfig.EnvironmentFile = "/run/bootstrap.env";}))
    (rejects "must not seed a shared bootstrap administrator" (app {services.keycloak.initialAdminPassword = "eval-only-placeholder";}))
    (rejects "must not contain CMS bootstrap settings" (app {sops.templates."terrace-website/cms.env".content = lib.mkAfter "CMS_BOOTSTRAP_ADMIN=1\n";}))
    # Listening sockets and the firewall.
    (rejects "keeps PostgreSQL and Keycloak (HTTP and management) on loopback" (app {services.postgresql.settings.listen_addresses = lib.mkForce "*";}))
    (rejects ": 5432 from" (app {networking.firewall.allowedTCPPorts = [5432];}))
    (rejects "refuses firewall ports or rules" (app {networking.firewall.extraCommands = "iptables -A INPUT -j ACCEPT";}))
    (rejects "refuses firewall ports or rules" (app {
      networking.nftables.tables.open = {
        family = "inet";
        content = "";
      };
    }))
    (rejectsRelay ": 51820 from" (relayAs "production" {networking.firewall.allowedUDPPorts = [51820];}))
    (rejectsRelay "keeps Headscale's API, metrics and gRPC on loopback" (relayAs "production" {services.headscale.settings.metrics_listen_addr = lib.mkForce "0.0.0.0:9090";}))
    (rejects "nginx vhosts" (app {services.nginx.virtualHosts."admin.terracefclub.org".locations."/".return = "404";}))
    # Accounts, SSH, sudo, kernel and emergency access.
    (rejects "accounts: mutable users" (app {users.mutableUsers = lib.mkForce true;}))
    (rejects "accounts: password field on operator" (app {users.users.operator.hashedPassword = "eval-only";}))
    (rejects "accounts: wheel members operator nobody" (app {users.groups.wheel.members = ["nobody"];}))
    (rejects "accounts: wheel members helper operator" (app {
      users.users.helper = {
        isNormalUser = true;
        extraGroups = ["wheel"];
      };
    }))
    (rejects "sudo rules" (app {
      security.sudo.extraRules = [
        {
          users = ["nobody"];
          commands = ["ALL"];
        }
      ];
    }))
    (rejects "free-form firewall, sshd or sudo configuration" (app {services.openssh.extraConfig = "PermitTunnel yes";}))
    (rejects "free-form firewall, sshd or sudo configuration" (app ./eval-fixtures/nixos/modules/imitation.nix))
    (rejects "free-form firewall, sshd or sudo configuration" (app {security.polkit.extraConfig = "polkit.addRule(function () { return polkit.Result.YES; });";}))
    (rejects "sudo as the only privilege tool" (app {security.doas.enable = true;}))
    (rejects "sudo as the only privilege tool" (app {security.sudo-rs.enable = true;}))
    (rejects "never inline or imported" (app {systemd.services.nscd.serviceConfig.SetCredential = "token:eval-only";}))
    (rejects "never inline or imported" (app {systemd.services.nscd.serviceConfig.ImportCredential = "token";}))
    (rejects "enabled services: memcached" (app {
      services.memcached = {
        enable = true;
        listen = "0.0.0.0";
        port = 11311;
      };
    }))
    (rejects "listening sockets: rogue" (app {
      systemd.sockets.rogue = {
        listenStreams = ["0.0.0.0:18000"];
        socketConfig.Accept = true;
      };
    }))
    (rejects "listeners" (app {systemd.services.terrace-web.environment.HOST = "0.0.0.0";}))
    (rejects "listeners" (app {systemd.services.terrace-web.serviceConfig.Environment = "HOST=0.0.0.0";}))
    (rejects "listeners" (app {sops.templates."terrace-website/web.env".content = lib.mkAfter "HOST=0.0.0.0\n";}))
    (rejects "Keycloak database init logs no statements" (app {systemd.services.keycloakPostgreSQLInit.environment.PGOPTIONS = lib.mkForce "-c log_statement=all";}))
    (rejects "unit dependencies: rogue.service" (app {systemd.services.sshd.wants = ["rogue.service"];}))
    (rejects "unit dependencies: rogue.service" (app {systemd.services.terrace-web.bindsTo = ["rogue.service"];}))
    (rejects "systemd packages: " (app ({pkgs, ...}: {systemd.packages = [pkgs.socat];})))
    (rejects "unit files through environment.etc: systemd/system/rogue.service" (app {environment.etc."systemd/system/rogue.service".text = "[Service]\nExecStart=/bin/sh\n";}))
    (rejects "runs no container or VM daemon" (app {virtualisation.docker.enable = true;}))
    (rejects "needs the primary backup target" (app {terrace.backup.targets.primary.enable = lib.mkForce false;}))
    (rejects "free-form firewall, sshd or sudo configuration" (app {services.nginx.appendHttpConfig = "server { listen 8443; }";}))
    (rejects "free-form firewall, sshd or sudo configuration" (app {services.nginx.streamConfig = "server { listen 2222; proxy_pass 127.0.0.1:22; }";}))
    (rejects "free-form firewall, sshd or sudo configuration" (app {
      services.nginx.defaultListen = [
        {
          addr = "0.0.0.0";
          port = 8080;
        }
      ];
    }))
    (rejects "free-form firewall, sshd or sudo configuration" (app {security.sudo.extraConfig = "nobody ALL=(ALL) NOPASSWD: ALL";}))
    (rejects "kernel debug or emergency access" (app {boot.kernelParams = ["systemd.debug-shell"];}))
    (rejects "no emergency shell" (app {systemd.enableEmergencyMode = lib.mkForce true;}))
    # Capacity and scope.
    (rejects "with 25 percent headroom the host needs 9216 MiB" (app {terrace.production.memoryMiB = lib.mkForce 8192;}))
    (rejects "does not yet enable K3s" (app {services.k3s.enable = true;}))
    (rejects "per-host hardware module" (app {boot.loader.systemd-boot.enable = lib.mkForce false;}))
  ];
in
  assert lib.assertMsg (positive.app == []) "Production app violations: ${toString positive.app}";
  assert lib.assertMsg (positive.static == []) "Static-network app violations: ${toString positive.static}";
  assert lib.assertMsg (positive.relayRehearsal == []) "Relay (rehearsal addressing) violations: ${toString positive.relayRehearsal}";
  assert lib.assertMsg (positive.rehearsal == []) "Rehearsal instance violations: ${toString positive.rehearsal}";
  assert lib.assertMsg (relayDocumentation == ["assertion: Terrace production relay needs its verified public IPv4 (no private, shared, loopback, link-local, benchmarking or documentation range)." "toplevel does not evaluate: Terrace production relay needs its verified public IPv4 (no private, shared, loopback, link-local, benchmarking or documentation range)."]) "Production relay with a documentation address: ${toString relayDocumentation}";
  assert lib.all lib.id negatives; "PASS: production app, static-network app, relay and rehearsal instance hold every invariant; ${toString (builtins.length negatives)} rejected variants"
