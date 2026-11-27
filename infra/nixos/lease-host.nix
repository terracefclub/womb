# One leased production app host on Hetzner Cloud (docs/cloud-rehearsal.md, "Production lease host"): the
# production app role at `instance = "rehearsal"` with synthetic `.invalid` names, manual TLS from a run CA, the
# Hetzner hardware module and the signed deploy path. It builds what `rescue_install.py` installs (generation V0,
# described by `install`) and the update variants the lease activates (V1 to V4 and the release change), all from
# run inputs passed on the command line and never committed: operator and deploy PUBLIC keys, the escrow
# recipient, the run CA certificate and the throwaway backup endpoint's host key.
{
  # The whole disk the Rescue probe recorded (`<phase>-app-disk.json`, field disk.device); `/dev/sda` on every
  # CPX server leased so far. The Disko script formats exactly this device.
  disk,
  # The run's installed operator key(s), `ssh-ed25519 <key> [comment]`; never the bootstrap root key.
  operatorKeys,
  # The run's throwaway escrow key: its public age recipient.
  escrowRecipient,
  # Exactly two Nix public keys trusted from the first install: the build machine's deploy key and the offline
  # spare (owner decision 2026-11-26).
  deployKeys,
  # The run CA's certificate (public; a path, copied into the store). The CMS, website and TLS expiry check trust
  # it; the leaf and its key reach the host as tls/certificate and tls/key with `terrace-secrets push`.
  runCa,
  # The primary backup repository: an SFTP endpoint reached on the host at backupAddress:backupPort (the near end
  # of a reverse SSH forward from the build machine), pinned to backupHostKey (`ssh-ed25519 <key>`). The
  # repository user and path are fixed: `sftp:terrace@<address>:app`.
  backupAddress ? "127.0.0.1",
  backupPort,
  backupHostKey,
  # The release pairs: installed at the previous kept release (680ad56, platform/production/deploy-previous-release.json),
  # updated to the pinned one (dd1aef7, platform/website-release.json), as in the deploy VM proof.
  webArchive ? null,
  cmsArchive ? null,
  previousWebArchive ? null,
  previousCmsArchive ? null,
  tfcSource ? null,
  # Evaluation checks pass prepared stand-ins instead of archives and source.
  release ? null,
  previous ? null,
  identityContract ? null,
  # The Womb commit being built (`git rev-parse HEAD`), recorded in each system manifest; informational.
  wombRevision ? null,
}: let
  install = builtins.getFlake "path:${toString ./.}";
  platform = builtins.getFlake "path:${toString ../../platform}";
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  lib = pkgs.lib;
  next =
    if release != null
    then release
    else import ../../platform/website-release.nix {inherit pkgs webArchive cmsArchive;};
  first =
    if previous != null
    then previous
    else
      import ../../platform/production/deploy-previous-release.nix {
        inherit pkgs;
        webArchive = previousWebArchive;
        cmsArchive = previousCmsArchive;
      };
  trust = "${runCa}";
  octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])";
  backupKey = builtins.match "(ssh-ed25519 [A-Za-z0-9+/]{68})" backupHostKey;
  hardwareModule = {
    imports = [install.nixosModules.hetzner-production-hardware];
    terrace.cloudDisk.device = disk;
  };
  localModule = {
    networking.hostName = "app";
    terrace.production = {
      instance = "rehearsal";
      memoryMiB = 15360;
      inherit operatorKeys escrowRecipient;
      deploy.trustedKeys = deployKeys;
      # Manual TLS (the default mode); the expiry check verifies against the run CA.
      tls.trustAnchorFile = trust;
      app = {
        webHostname = "web.terrace.invalid";
        cmsHostname = "cms.terrace.invalid";
        identityHostname = "identity.terrace.invalid";
        # Not the pinned revision, so the list is not the default: activate and rollback compare it with the ledger.
        releaseMigrations = first.migrations;
        backup.primary = {
          repository = "sftp:terrace@${backupAddress}:app";
          port = backupPort;
          # ssh -p <port> looks the host up as [address]:port.
          knownHosts = "[${backupAddress}]:${toString backupPort} ${builtins.head backupKey}";
        };
      };
    };
    # Server-to-server calls to the public origins (OIDC discovery and introspection) go to this host's nginx,
    # which serves the run CA's leaf. The rehearsal instance accepts this; production refuses it.
    systemd.services = lib.genAttrs ["terrace-cms" "terrace-cms-migrate" "terrace-cms-sweep" "terrace-web"] (_: {
      environment.NODE_EXTRA_CA_CERTS = trust;
    });
  };
  v0 = import ./production-config.nix {
    role = "app";
    inherit hardwareModule localModule tfcSource identityContract wombRevision;
    release = first;
  };
  extend = system: modules: system.extendModules {inherit modules;};
  marker = text: {environment.etc."terrace-lease-variant".text = text + "\n";};
  # Each variant extends the one the lease runs before it, so it differs from the running system only by its change.
  systems = rec {
    inherit v0;
    # V1: a plain switch (an /etc file), at the installed release.
    v1 = extend v0 [(marker "v1")];
    # The release change with its CMS migration (680ad56 to dd1aef7), from V0 (V1 is rolled back first).
    release = extend v0 [
      {
        terrace.website = {
          webPackage = lib.mkForce next.web;
          cmsPackage = lib.mkForce next.cms;
          revision = lib.mkForce next.revision;
        };
        terrace.production.app.releaseMigrations = lib.mkForce next.migrations;
      }
    ];
    # V2: one more initrd driver, so the initrd differs and `activate` demands --boot and a reboot.
    v2 = extend release [
      (marker "v2")
      {boot.initrd.availableKernelModules = ["virtio_input"];}
    ];
    # V3: an sshd setting (not free-form configuration): the switch restarts sshd (its restart trigger is sshd_config).
    v3 = extend v2 [
      (marker "v3")
      {services.openssh.settings.LogLevel = "VERBOSE";}
    ];
    # V4: a unit that fails and restarts, wanted at boot: the switch registers the generation, then fails (exit 4),
    # as in the update drill. The evaluation check rejects it for exactly this unit.
    v4 = extend v3 [
      (marker "v4")
      {
        systemd.services.terrace-lease-broken = {
          wantedBy = ["multi-user.target"];
          serviceConfig = {
            ExecStart = "${pkgs.coreutils}/bin/false";
            Restart = "on-failure";
            RestartSec = 30;
          };
        };
      }
    ];
  };
  built = system: system.config.system.build;
  # Exactly what `lease_install_plan.py` turns into `<phase>-app-install.json` once the disk receipt exists.
  installOutputs = {
    schema = 1;
    role = "app";
    device = disk;
    system = (built v0).toplevel;
    disko = (built v0).diskoScript;
    installer = (built v0).nixos-install;
  };
in
  assert lib.assertMsg (builtins.length deployKeys == 2 && lib.unique deployKeys == deployKeys) "The lease host trusts exactly two distinct deploy keys: the build machine's and the offline spare.";
  assert lib.assertMsg (builtins.isInt backupPort && backupPort >= 1024 && backupPort <= 65535) "The backup port is the unprivileged near end of a reverse forward (1024-65535).";
  assert lib.assertMsg (builtins.match "${octet}\\.${octet}\\.${octet}\\.${octet}" backupAddress != null) "The backup address is a dotted IPv4 address.";
  assert lib.assertMsg (backupKey != null) "The backup host key is exactly `ssh-ed25519 <key>`.";
  assert lib.assertMsg (lib.hasPrefix builtins.storeDir trust) "Pass the run CA certificate as a path (--arg runCa /path/to/ca.pem), not a string."; {
    inherit systems installOutputs;
    install = pkgs.writeText "terrace-lease-app-install.json" (builtins.toJSON installOutputs + "\n");
    variants = pkgs.writeText "terrace-lease-app-variants.json" (builtins.toJSON (lib.mapAttrs (_: system: (built system).toplevel) systems) + "\n");
  }
