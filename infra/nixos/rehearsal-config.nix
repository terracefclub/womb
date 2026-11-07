{
  role,
  localModule,
  privateIPv4 ? null,
  webArchive ? null,
  cmsArchive ? null,
  tfcSource ? null,
}: let
  install = builtins.getFlake "path:${toString ./.}";
  platform = builtins.getFlake "path:${toString ../../platform}";
  system = "x86_64-linux";
  pkgs = import platform.inputs.nixpkgs {inherit system;};
  roles = {
    gateway = import ../../platform/cloud-rehearsal/gateway.nix {
      release = import ../../platform/website-release.nix {inherit pkgs webArchive cmsArchive;};
      contract = import ../../platform/test-identity-contract.nix {inherit pkgs tfcSource;};
    };
    relay = import ../../platform/cloud-rehearsal/relay.nix {inherit privateIPv4;};
  };
in
  assert pkgs.lib.assertMsg (builtins.hasAttr role roles) "Select the disposable gateway or relay role.";
  assert pkgs.lib.assertMsg (role != "gateway" || (webArchive != null && cmsArchive != null && tfcSource != null)) "Gateway requires the reviewed release pair and TFC contract source.";
  assert pkgs.lib.assertMsg (role != "relay" || privateIPv4 != null) "Relay requires its verified private IPv4.";
    platform.inputs.nixpkgs.lib.nixosSystem {
      inherit system;
      modules = [install.nixosModules.cloud-disk roles.${role} localModule];
    }
