# A production host on Hetzner Cloud: the pinned cloud disk module (the only
# hardware-specific part), one production role and a private host module with
# the deployment's parameters (never secrets). An on-prem host uses the same
# role with its own hardware module instead of cloud-disk.
{
  role,
  localModule,
  webArchive ? null,
  cmsArchive ? null,
  tfcSource ? null,
  # Evaluation checks pass prepared stand-ins instead of archives and source.
  release ? null,
  identityContract ? null,
}: let
  install = builtins.getFlake "path:${toString ./.}";
  platform = builtins.getFlake "path:${toString ../../platform}";
  system = "x86_64-linux";
  pkgs = import platform.inputs.nixpkgs {inherit system;};
  lib = pkgs.lib;
  bundles =
    if release != null
    then release
    else import ../../platform/website-release.nix {inherit pkgs webArchive cmsArchive;};
  contract =
    if identityContract != null
    then identityContract
    else import ../../platform/identity-contract.nix {inherit pkgs tfcSource;};
  roles = {
    app = [
      platform.nixosModules.production-app
      {
        terrace.production.app.identityContract = contract;
        terrace.website = {
          webPackage = bundles.web;
          cmsPackage = bundles.cms;
          inherit (bundles) revision;
        };
      }
    ];
    relay = [platform.nixosModules.production-relay];
  };
in
  assert lib.assertMsg (builtins.hasAttr role roles) "Select the production app or relay role.";
  assert lib.assertMsg (role != "app" || release != null || (webArchive != null && cmsArchive != null)) "The app role requires the reviewed release pair.";
  assert lib.assertMsg (role != "app" || identityContract != null || tfcSource != null) "The app role requires the TFC source at the released revision (identity contract).";
    platform.inputs.nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [install.nixosModules.cloud-disk localModule] ++ roles.${role};
    }
