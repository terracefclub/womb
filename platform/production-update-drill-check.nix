{
  webArchive,
  cmsArchive,
  tfcSource,
  previousWebArchive,
  previousCmsArchive,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  previous = import ./production/deploy-previous-release.nix {
    inherit pkgs;
    webArchive = previousWebArchive;
    cmsArchive = previousCmsArchive;
  };
  contract = import ./identity-contract.nix {inherit pkgs tfcSource;};
in
  pkgs.testers.runNixOSTest (import ./production/update-drill-test.nix {
    inherit pkgs release previous contract;
    sopsModule = platform.inputs.sops-nix.nixosModules.sops;
  })
