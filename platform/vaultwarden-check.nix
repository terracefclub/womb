let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
in
  pkgs.testers.runNixOSTest (import ./vaultwarden-test.nix {inherit pkgs;})
