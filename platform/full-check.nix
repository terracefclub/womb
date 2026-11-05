{
  webArchive,
  cmsArchive,
  tfcSource,
  runId,
}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  release = import ./website-release.nix {inherit pkgs webArchive cmsArchive;};
  contract = import ./test-identity-contract.nix {inherit pkgs tfcSource;};
in
  assert builtins.match "[a-f0-9]{32}" runId != null;
    pkgs.testers.runNixOSTest (import ./full/test.nix {inherit pkgs release contract runId;})
