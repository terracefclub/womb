{
  webArchive,
  cmsArchive,
  tfcSource,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  contract = import ./test-identity-contract.nix {inherit pkgs tfcSource;};
in
  pkgs.testers.runNixOSTest (import ./website-gateway-test.nix {inherit pkgs release contract;})
