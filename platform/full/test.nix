{
  pkgs,
  release,
  contract,
  runId,
  withDocumenso ? false,
  withRelayPeers ? false,
}: let
  documensoFixture = import ../documenso/fixture-package.nix {inherit pkgs contract;};
  gateway = import ../website-gateway-test.nix {
    inherit pkgs release contract;
    extraDnsNames = pkgs.lib.optional withDocumenso "documents.terrace.invalid";
    fixtureProgram =
      if withDocumenso
      then "${documensoFixture}/functional-fixture.ts"
      else "${contract}/fixture.ts";
  };
  relay = import ../relay-test.nix {
    inherit pkgs;
    module = import ../relay.nix;
  };
  haTest = pkgs.writeShellScriptBin "demos-ha-test" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.aiohttp]))}/bin/python3 ${../demos-ha-test.py} "$@"
  '';
  browserTest = pkgs.writeShellScriptBin "rehearsal-browser" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.playwright]))}/bin/python3 ${../website-gateway/browser.py}
  '';
  documensoBrowser = pkgs.writeShellScriptBin "rehearsal-documenso-browser" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.playwright]))}/bin/python3 ${../documenso/functional-browser.py} "$@"
  '';
  cgroupCheck = pkgs.writeShellApplication {
    name = "rehearsal-cgroups-empty";
    runtimeInputs = [pkgs.findutils pkgs.coreutils];
    text = builtins.readFile ./empty-cgroups.sh;
  };
  image = pkgs.dockerTools.buildImage {
    name = "terrace-persistence-probe";
    tag = "v1";
    copyToRoot = pkgs.buildEnv {
      name = "terrace-rehearsal-probe-root";
      paths = [pkgs.pkgsStatic.busybox];
      pathsToLink = ["/bin"];
    };
  };
  common = {
    config,
    lib,
    ...
  }: {
    virtualisation.diskImage = "$TERRACE_REHEARSAL_RUN_DIR/state/${config.networking.hostName}.qcow2";
    virtualisation.qemu.options = ["-fw_cfg" "name=opt/terrace-rehearsal,string=${runId}"];
    environment.systemPackages = [pkgs.curl pkgs.jq pkgs.openssh pkgs.restic pkgs.python3 pkgs.netcat-openbsd cgroupCheck];
    environment.etc."rehearsal-versions.json".text = builtins.toJSON ({
        keycloak = pkgs.keycloak.version;
        homeAssistant = pkgs.home-assistant.version;
        frigate = pkgs.frigate.version;
        go2rtc = pkgs.go2rtc.version;
        headscale = pkgs.headscale.version;
      }
      // lib.optionalAttrs withDocumenso {documenso = (builtins.fromJSON (builtins.readFile ../documenso/image.json)).version;});
    systemd.tmpfiles.rules = ["f /var/lib/unrelated-synthetic-sentinel 0600 root root - retained"];
  };
  backup = {lib, ...}: {
    imports = [../backup.nix common];
    systemd.timers = lib.genAttrs ["terrace-backup" "terrace-backup-check" "terrace-backup-freshness"] (_: {wantedBy = lib.mkForce [];});
    terrace.backup = {
      enable = true;
      check.readDataSubset = "100%";
      targets.primary = {
        enable = true;
        repository = "sftp:restic@client:repository";
        passwordFile = "/var/lib/rehearsal-escrow/repository-password";
        sftp = {
          identityFile = "/var/lib/rehearsal-escrow/id_ed25519";
          knownHostsFile = "/var/lib/rehearsal-escrow/known_hosts";
        };
      };
    };
  };
in {
  name = "terrace-established-services-rehearsal";
  nodes = {
    gateway = {
      config,
      lib,
      ...
    }: {
      imports = [gateway.nodes.gateway ../single-node.nix ../documenso.nix backup] ++ lib.optional withRelayPeers ./relay-peers.nix;
      virtualisation.memorySize = lib.mkForce 8192;
      virtualisation.diskSize = 8192;
      terrace.singleNode.enable = true;
      terrace.documenso = lib.mkIf withDocumenso {
        enable = true;
        hostname = "documents.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        certificateFile = "/var/lib/terrace-gateway-test/cert.pem";
        certificateKeyFile = "/var/lib/terrace-gateway-test/key.pem";
        identityCaFile = "/var/lib/terrace-gateway-test/cert.pem";
      };
      services.k3s.images = [pkgs.k3s.airgap-images image];
      networking.firewall.allowedTCPPorts = lib.optional withDocumenso 13050;
      services.postgresql = {
        package = lib.mkForce pkgs.postgresql_18;
        settings = {
          max_connections = lib.mkForce 64;
          shared_buffers = lib.mkForce "128MB";
          work_mem = "2MB";
        };
      };
      terrace.backup = {
        postgresql.databases = ["keycloak"];
        extraPaths =
          {
            identity-inputs = "/var/lib/terrace-gateway-test";
            k3s-server = "/var/lib/rancher/k3s/server";
            k3s-storage = "/var/lib/rancher/k3s/storage";
            k3s-node = "/etc/rancher/node";
          }
          // lib.optionalAttrs withRelayPeers {
            tailnet-state = "/var/lib/tailscale";
            tailnet-inputs = "/var/lib/rehearsal-tailnet";
          };
      };
      # Blank boot does not start writers. The driver performs explicit bootstrap.
      systemd.services =
        lib.genAttrs ["keycloak" "nginx" "k3s"] (_: {wantedBy = lib.mkForce [];})
        // {
          postgresql = {
            wantedBy = lib.mkForce [];
            serviceConfig.MemoryMax = lib.mkForce "1536M";
          };
          gateway-test-inputs.serviceConfig.ExecCondition = "${pkgs.writeShellScript "rehearsal-inputs-create-only" ''
            test ! -e /var/lib/terrace-gateway-test/terrace-realm.json
          ''}";
          gateway-test-inputs.path = lib.optional withDocumenso pkgs.openssl;
        }
        // lib.optionalAttrs withDocumenso {
          terrace-documenso.wantedBy = lib.mkForce [];
        };
      systemd.targets.postgresql.wantedBy = lib.mkForce [];
      systemd.timers.terrace-identity-health.wantedBy = lib.mkForce [];
      environment.etc =
        {
          "rehearsal-pg-directory".text = config.services.postgresql.dataDir;
          "rehearsal-probe.yaml".source = ./probe.yaml;
        }
        // lib.optionalAttrs withDocumenso {"documenso-recovery.py".source = ../documenso/recovery-auth-test.py;};
    };
    automation = {
      nodes,
      lib,
      ...
    }: {
      imports = [../demos.nix backup] ++ lib.optional withRelayPeers ./relay-peers.nix;
      virtualisation = {
        memorySize = 4096;
        diskSize = 4096;
        cores = 2;
      };
      terrace.demos.enable = true;
      terrace.backup.extraPaths =
        {
          home-assistant = "/var/lib/terrace-demo-hass";
          frigate = "/var/lib/terrace-demo-frigate";
          fixture-token = "/var/lib/rehearsal-demo-token";
        }
        // lib.optionalAttrs withRelayPeers {
          tailnet-state = "/var/lib/tailscale";
          tailnet-inputs = "/var/lib/rehearsal-tailnet";
        };
      environment.systemPackages = [haTest pkgs.sqlite];
      systemd.services = lib.genAttrs ["go2rtc" "frigate" "nginx" "home-assistant"] (_: {wantedBy = lib.mkForce [];});
      systemd.sockets.terrace-demo-ha.wantedBy = lib.mkForce [];
      # Upstream's target independently pulls in the service even when its own
      # wantedBy is empty. Neither initial nor replacement boot may start HA.
      systemd.targets.home-assistant.wantedBy = lib.mkForce [];
      security.pki.certificateFiles = lib.optional withDocumenso (builtins.head nodes.gateway.security.pki.certificateFiles);
      networking.hosts = lib.optionalAttrs withDocumenso {${nodes.gateway.networking.primaryIPAddress} = ["documents.terrace.invalid"];};
    };
    relay = {
      config,
      lib,
      ...
    }: {
      imports = [relay.nodes.relay backup];
      terrace.relay.publicIPv4 = lib.mkForce config.networking.primaryIPAddress;
      terrace.relay.policy = lib.mkIf withRelayPeers {
        tagOwners = lib.genAttrs ["tag:rehearsal-application" "tag:rehearsal-private" "tag:rehearsal-denied"] (_: ["rehearsal-synthetic@"]);
        acls = [
          {
            action = "accept";
            src = ["tag:rehearsal-application"];
            dst = ["tag:rehearsal-private:18444"];
          }
        ];
      };
      virtualisation = {
        memorySize = 1024;
        diskSize = 2048;
        cores = 1;
      };
      systemd.services = lib.genAttrs ["headscale" "nginx"] (_: {wantedBy = lib.mkForce [];});
    };
    client = {
      nodes,
      lib,
      ...
    }: {
      imports = [gateway.nodes.client common] ++ lib.optional withRelayPeers ./relay-peers.nix;
      environment.systemPackages = [browserTest] ++ lib.optional withDocumenso documensoBrowser;
      virtualisation.memorySize = lib.mkForce 2048;
      virtualisation.diskSize = 8192;
      services.openssh = {
        enable = true;
        settings.PasswordAuthentication = false;
        extraConfig = ''
          Match User restic
            ForceCommand internal-sftp
            AllowTcpForwarding no
            PermitTTY no
        '';
      };
      users.users.restic.isNormalUser = true;
      security.pki.certificateFiles = [nodes.relay.services.nginx.virtualHosts.relay.sslCertificate];
      environment.etc."rehearsal-browser-ca.pem".source = builtins.head nodes.gateway.security.pki.certificateFiles;
    };
  };
  testScript =
    "DOCUMENSO_ENABLED = ${
      if withDocumenso
      then "True"
      else "False"
    }\n"
    + "RELAY_PEERS_ENABLED = ${
      if withRelayPeers
      then "True"
      else "False"
    }\n"
    + builtins.readFile ./scenario.py;
}
