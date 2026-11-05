let
  platform = builtins.getFlake (toString ./.);
  pkgs = import platform.inputs.nixpkgs {system = "x86_64-linux";};
in
  import ./windmill/image.nix {inherit pkgs;}
