{
  webArchive,
  cmsArchive,
  tfcSource,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  contract = import ./identity-contract.nix {inherit pkgs tfcSource;};
in
  pkgs.testers.runNixOSTest (import ./production/test.nix {
    inherit pkgs release contract;
    sopsModule = platform.inputs.sops-nix.nixosModules.sops;
  })
