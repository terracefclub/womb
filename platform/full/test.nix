{
  pkgs,
  release,
  contract,
  runId,
  withDocumenso ? false,
  withRelayPeers ? false,
  withDocumensoSigning ? false,
  withApplications ? false,
}: let
  documensoFixture = import ../documenso/fixture-package.nix {inherit pkgs contract;};
  applicationFixture = import ./fixture-package.nix {inherit pkgs contract;};
  windmillRoster = import ../windmill-functional/roster-package.nix {inherit pkgs;};
  gateway = import ../website-gateway-test.nix {
    inherit pkgs release contract;
    extraDnsNames = pkgs.lib.optional withDocumenso "documents.terrace.invalid" ++ pkgs.lib.optionals withApplications ["windmill.terrace.invalid" "vault.terrace.invalid"];
    tlsFixture =
      if withApplications
      then
        import ./certificate.nix {
          inherit pkgs;
          hostnames = ["web.terrace.invalid" "cms.terrace.invalid" "identity.terrace.invalid" "documents.terrace.invalid" "windmill.terrace.invalid" "vault.terrace.invalid"];
        }
      else null;
    fixtureProgram =
      if withApplications
      then "${applicationFixture}/full/fixture.ts"
      else if withDocumenso
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
  windmillBrowser = pkgs.writeShellScriptBin "rehearsal-windmill-browser" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.playwright]))}/bin/python3 ${../windmill-functional/functional-browser.py} "$@"
  '';
  vaultwardenBrowser = pkgs.writeShellScriptBin "rehearsal-vaultwarden-browser" ''
    exec ${(pkgs.python3.withPackages (ps: [ps.playwright]))}/bin/python3 ${../vaultwarden/browser.py} "$@"
  '';
  cgroupCheck = pkgs.writeShellApplication {
    name = "rehearsal-cgroups-empty";
    runtimeInputs = [pkgs.findutils pkgs.coreutils];
    text = builtins.readFile ./empty-cgroups.sh;
  };
  image = import ./probe-image.nix {inherit pkgs;};
  common = {
    config,
    lib,
    ...
  }: {
    virtualisation.diskImage = "$TERRACE_REHEARSAL_RUN_DIR/state/${config.networking.hostName}.qcow2";
    virtualisation.qemu.options = ["-fw_cfg" "name=opt/terrace-rehearsal,string=${runId}"];
    environment.etc."rehearsal-restore-evidence.py".source = ./restore-evidence.py;
    environment.systemPackages = [pkgs.curl pkgs.jq pkgs.openssh pkgs.restic pkgs.python3 pkgs.netcat-openbsd cgroupCheck];
    environment.etc."rehearsal-versions.json".text = builtins.toJSON ({
        keycloak = pkgs.keycloak.version;
        homeAssistant = pkgs.home-assistant.version;
        frigate = pkgs.frigate.version;
        go2rtc = pkgs.go2rtc.version;
        headscale = pkgs.headscale.version;
      }
      // lib.optionalAttrs withDocumenso {documenso = (builtins.fromJSON (builtins.readFile ../documenso/image.json)).version;}
      // lib.optionalAttrs withApplications {
        windmill = (builtins.fromJSON (builtins.readFile ../windmill/release.json)).version;
        vaultwarden = (builtins.fromJSON (builtins.readFile ../vaultwarden/release.json)).version;
        vaultwardenWebVault = (builtins.fromJSON (builtins.readFile ../vaultwarden/release.json)).webvaultVersion;
      });
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
      imports = [gateway.nodes.gateway ../single-node.nix ../documenso.nix backup] ++ lib.optional withRelayPeers ./relay-peers.nix ++ lib.optional withDocumensoSigning ../documenso/mailpit-test.nix ++ lib.optional withApplications ./applications.nix;
      # Test isolation capacity, not a production hardware requirement. Both
      # pinned OCI images are imported again on the blank replacement disk.
      virtualisation.memorySize = lib.mkForce (
        if withApplications
        then 12288
        else 8192
      );
      virtualisation.cores = lib.mkIf withApplications (lib.mkForce 4);
      virtualisation.diskSize =
        if withApplications
        then 24576
        else 8192;
      terrace.singleNode.enable = true;
      terrace.documenso = lib.mkIf withDocumenso {
        enable = true;
        enableSigning = withDocumensoSigning;
        hostname = "documents.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        certificateFile = "/var/lib/terrace-gateway-test/cert.pem";
        certificateKeyFile = "/var/lib/terrace-gateway-test/key.pem";
        identityCaFile = "/var/lib/terrace-gateway-test/${
          if withApplications
          then "ca.pem"
          else "cert.pem"
        }";
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
        // lib.optionalAttrs withApplications {
          # Keep runtime client demand below the shared64-slot server limit.
          # The upstream identity init must create its password-bearing role
          # before applying this cap; pre-creating it would skip that setup.
          # Its CREATEDB setup privilege is unnecessary after DB creation.
          keycloakPostgreSQLInit.postStart = ''
            psql -X -w -v ON_ERROR_STOP=1 -d postgres -c 'ALTER ROLE keycloak NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 10; ALTER ROLE terrace_cms CONNECTION LIMIT 10;' >/dev/null 2>&1
          '';
          rehearsal-pg-sample = {
            after = ["postgresql.target"];
            requires = ["postgresql.target"];
            path = [config.services.postgresql.package];
            serviceConfig = {
              Type = "exec";
              User = "postgres";
              Group = "postgres";
              RuntimeDirectory = "rehearsal-pg-sample";
              RuntimeDirectoryMode = "0700";
              RuntimeDirectoryPreserve = "yes";
              ExecStart = "${pkgs.python3}/bin/python3 ${./pg-sample.py}";
              RuntimeMaxSec = 360;
              TimeoutStopSec = 10;
              KillMode = "control-group";
              StandardOutput = "null";
              StandardError = "null";
              UMask = "0077";
              MemoryMax = "64M";
              TasksMax = 8;
              NoNewPrivileges = true;
            };
          };
        }
        // lib.optionalAttrs withDocumenso {
          terrace-documenso.wantedBy = lib.mkForce [];
        };
      # Only the synthetic signing variant retains bounded private app errors.
      virtualisation.oci-containers.containers = lib.optionalAttrs withDocumensoSigning {
        terrace-documenso = {
          log-driver = lib.mkForce "k8s-file";
          extraOptions = lib.mkAfter ["--log-opt=path=/var/lib/documenso-test-evidence/app.log" "--log-opt=max-size=4194304"];
        };
      };
      systemd.tmpfiles.rules = lib.optionals withDocumensoSigning [
        "d /var/lib/documenso-test-evidence 0700 root root -"
        "f /var/lib/documenso-test-evidence/app.log 0600 root root -"
      ];
      systemd.targets.postgresql.wantedBy = lib.mkForce [];
      systemd.timers.terrace-identity-health.wantedBy = lib.mkForce [];
      environment.etc =
        {
          "rehearsal-pg-directory".text = config.services.postgresql.dataDir;
          "rehearsal-probe.yaml".source = ./probe.yaml;
        }
        // lib.optionalAttrs withDocumenso {"documenso-recovery.py".source = ../documenso/recovery-auth-test.py;}
        // lib.optionalAttrs withDocumensoSigning {
          "documenso-signing-proof.py".source = ../documenso/signing-proof.py;
          "documenso-smtp-probe.cjs".source = ../documenso/smtp-probe.cjs;
        }
        // lib.optionalAttrs withApplications {
          "vaultwarden-attachments.py".source = ../vaultwarden/attachment-files.py;
          "windmill-evidence/capture.py".source = ../windmill/capture.py;
          "windmill-evidence/diagnostics.py".source = ../windmill/diagnostics.py;
          "windmill-evidence/vaultwarden-capture.py".source = ../vaultwarden/capture.py;
        };
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
      config,
      nodes,
      lib,
      ...
    }: {
      imports = [gateway.nodes.client common] ++ lib.optional withRelayPeers ./relay-peers.nix;
      environment.systemPackages = [browserTest] ++ lib.optional withDocumenso documensoBrowser ++ lib.optional withDocumensoSigning pkgs.openssl ++ lib.optionals withApplications [windmillBrowser vaultwardenBrowser];
      virtualisation.memorySize = lib.mkForce (
        if withApplications
        then 6144
        else 2048
      );
      virtualisation.cores = lib.mkIf withApplications (lib.mkForce 4);
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
      environment.etc =
        {"rehearsal-browser-ca.pem".source = builtins.head nodes.gateway.security.pki.certificateFiles;}
        // lib.optionalAttrs withDocumensoSigning {"documenso-signing-proof.py".source = ../documenso/signing-proof.py;}
        // lib.optionalAttrs withApplications {"windmill-roster".source = windmillRoster;};
      systemd.services.rehearsal-concurrent = lib.mkIf withApplications {
        description = "Bounded simultaneous synthetic native browser checks";
        path = config.environment.systemPackages;
        environment = {
          TERRACE_BROWSER_BINARY = config.environment.variables.TERRACE_BROWSER_BINARY;
          HOME = "/root";
        };
        serviceConfig = {
          Type = "exec";
          ExecStart = "${pkgs.python3}/bin/python3 ${./concurrent.py}";
          KillMode = "control-group";
          RuntimeMaxSec = 330;
          TimeoutStopSec = 15;
          StandardOutput = "null";
          StandardError = "null";
          UMask = "0077";
          MemoryMax = "5G";
          CPUQuota = "400%";
          TasksMax = 1536;
        };
      };
    };
  };
  testScript =
    "APPLICATIONS_ENABLED = ${
      if withApplications
      then "True"
      else "False"
    }\n"
    + "DOCUMENSO_ENABLED = ${
      if withDocumenso
      then "True"
      else "False"
    }\n"
    + "DOCUMENSO_SIGNING_ENABLED = ${
      if withDocumensoSigning
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
