{tfcSource}: let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
  contract = import ./test-identity-contract.nix {inherit pkgs tfcSource;};
in
  pkgs.testers.runNixOSTest (import ./vaultwarden-functional-test.nix {inherit pkgs contract;})
