{
  webArchive,
  cmsArchive,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  # Evaluate composition without starting a provider or reading any runtime input.
  combined = platform.inputs.nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [
      ./website.nix
      ./identity.nix
      {
        system.stateVersion = "26.11";
        fileSystems."/" = {
          device = "/dev/vda";
          fsType = "ext4";
        };
        boot.loader.grub.enable = false;
        terrace.website = {
          enable = true;
          webPackage = release.web;
          cmsPackage = release.cms;
        };
        terrace.identity = {
          enable = true;
          hostname = "identity.terrace.invalid";
          realmFile = "/run/terrace-identity/terrace-realm.json";
          databasePasswordFile = "/run/terrace-identity/db-password";
          certificateFile = "/run/terrace-identity/cert.pem";
          certificateKeyFile = "/run/terrace-identity/key.pem";
        };
      }
    ];
  };
in
  assert builtins.isString combined.config.system.build.toplevel.drvPath;
    pkgs.testers.runNixOSTest (import ./website-test.nix {
      inherit pkgs release;
      module = import ./website.nix;
    })
